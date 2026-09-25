"""数据工具与缓存"""
import json
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Generic, TypeVar

import numpy as np
import pyarrow as pa
from pyarrow import ipc
from vnpy.trader.constant import Exchange, Interval, OptionType, Product
from vnpy.trader.database import DB_TZ, BaseDatabase
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.object import BarData, ContractData, HistoryRequest, TickData
from vnpy.trader.utility import extract_vt_symbol, get_file_path

from .base import CacheFlag
from .object import Clock, Snapshot

TICK_COLUMNS: dict[str, str] = {
    "last": "last_price",
    "bid1": "bid_price_1",
    "ask1": "ask_price_1",
    "bid_vol1": "bid_volume_1",
    "ask_vol1": "ask_volume_1",
    "volume": "volume",
    "turnover": "turnover",
    "open_interest": "open_interest",
    "limit_up": "limit_up",
    "limit_down": "limit_down",
}
TICK_PRICE_COLUMNS: set[str] = {"last", "bid1", "ask1", "limit_up", "limit_down"}


class OptionBarGenerator:
    """期权 K 线生成器"""

    def __init__(self, on_bars: Callable) -> None:
        """构造函数"""
        self.on_bars: Callable = on_bars

        self.bars: dict[str, BarData] = {}
        self.last_ticks: dict[str, TickData] = {}
        self.minute: datetime | None = None
        self.minute_end: datetime | None = None

    def update_tick(self, tick: TickData) -> None:
        """更新行情切片数据"""
        if not tick.last_price:
            return

        if self.minute_end is None or tick.datetime >= self.minute_end:
            self.flush()
            self.minute = tick.datetime.replace(second=0, microsecond=0)
            self.minute_end = self.minute + timedelta(minutes=1)

        bar: BarData | None = self.bars.get(tick.vt_symbol, None)
        if not bar:
            bar = BarData(
                symbol=tick.symbol,
                exchange=tick.exchange,
                interval=Interval.MINUTE,
                datetime=tick.datetime,
                gateway_name=tick.gateway_name,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
                open_interest=tick.open_interest
            )
            self.bars[bar.vt_symbol] = bar
        else:
            bar.high_price = max(bar.high_price, tick.last_price)
            bar.low_price = min(bar.low_price, tick.last_price)
            bar.close_price = tick.last_price
            bar.open_interest = tick.open_interest

        last_tick: TickData | None = self.last_ticks.get(tick.vt_symbol, None)
        if last_tick:
            bar.volume += max(tick.volume - last_tick.volume, 0)
            bar.turnover += max(tick.turnover - last_tick.turnover, 0)

        self.last_ticks[tick.vt_symbol] = tick

    def flush(self) -> None:
        """推送当前分钟的 K 线切片"""
        if not self.bars:
            return
        for bar in self.bars.values():
            bar.datetime = self.minute
        self.on_bars(self.bars)
        self.bars = {}

    def close_due(self, now: datetime, grace_seconds: float = 2.0) -> None:
        """按时钟收 K 线"""
        if self.bars and self.minute and now >= self.minute.astimezone().replace(tzinfo=None) + timedelta(seconds=60 + grace_seconds):
            self.flush()


class TickSnapshotSource:
    """tick 模式的快照来源"""

    def __init__(self, clock: Clock, slots: list[str]) -> None:
        self.clock = clock
        self.slots = slots
        self.slot_index = {vt_symbol: i for i, vt_symbol in enumerate(slots)}
        self.columns = {name: np.full(len(slots), np.nan) for name in TICK_COLUMNS}
        self.fields = [(name, field, name in TICK_PRICE_COLUMNS) for name, field in TICK_COLUMNS.items()]
        self.seq = 0
        self.dirty = False
        self.snapshot: Snapshot | None = None

    def update_tick(self, tick: TickData) -> None:
        slot = self.slot_index.get(tick.vt_symbol)
        if slot is None:
            return
        columns = self.columns
        up, down = tick.limit_up, tick.limit_down
        limited = up > 0
        for name, field, is_price in self.fields:
            value = getattr(tick, field)
            if is_price and (value <= 0 or (limited and not down <= value <= up)):
                value = np.nan
            columns[name][slot] = value
        self.dirty = True

    def latest(self) -> Snapshot | None:
        if self.dirty:
            self.seq += 1
            self.snapshot = Snapshot(
                columns={name: array.copy() for name, array in self.columns.items()},
                seq=self.seq, datetime=self.clock.now(), slots=self.slots,
            )
            self.dirty = False
        return self.snapshot


def naive_time(dt: datetime) -> datetime:
    """去掉时区的数据库时间"""
    return dt.astimezone(DB_TZ).replace(tzinfo=None)


def _nanoseconds(dt: datetime) -> int:
    """时间换成纳秒"""
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def _from_nanoseconds(ns: int) -> datetime:
    """纳秒换回时间"""
    return datetime.fromtimestamp(ns // 1_000_000_000, DB_TZ).replace(microsecond=ns % 1_000_000_000 // 1000)


def _contract_to_dict(contract: ContractData) -> dict:
    """合约转成字典"""
    data: dict = {}
    for item in fields(ContractData):
        if not item.init:
            continue
        value = getattr(contract, item.name)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, datetime):
            value = value.isoformat()
        data[item.name] = value
    return data


def _contract_from_dict(data: dict) -> ContractData:
    kwargs: dict = dict(data)
    kwargs["exchange"] = Exchange(kwargs["exchange"])
    kwargs["product"] = Product(kwargs["product"])
    if kwargs["option_type"]:
        kwargs["option_type"] = OptionType(kwargs["option_type"])
    for name in ("option_listed", "option_expiry"):
        if kwargs[name]:
            kwargs[name] = datetime.fromisoformat(kwargs[name])
    return ContractData(**kwargs)


def same_contracts(first: list[ContractData], second: list[ContractData]) -> bool:
    """两份合约主档是否相同（按存进缓存的字段比）"""
    return [_contract_to_dict(c) for c in first] == [_contract_to_dict(c) for c in second]


CACHE_FOLDER: str = "option_strategy_cache"


def default_cache_path() -> Path:
    """默认缓存目录"""
    return get_file_path(CACHE_FOLDER)


MINUTE_PRICE_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "bid1", "ask1", "settlement")


MINUTE_VOLUME_FIELDS: tuple[str, ...] = ("volume", "turnover", "open_interest", "bid_vol1", "ask_vol1")


def _arrow_type(name: str) -> pa.DataType:
    """字段的存储类型"""
    if name == "flags":
        return pa.uint8()
    if name == "turnover":
        return pa.float64()
    if name in MINUTE_VOLUME_FIELDS:
        return pa.int32()
    return pa.float32()


D = TypeVar("D")


class DayCache(ABC, Generic[D]):
    """按交易日存取的缓存仓"""

    version: int = 1

    def __init__(self, path: Path, memory: bool = False) -> None:
        self.path: Path = Path(path)
        self.memory: bool = memory
        self.loaded: dict[date, D] = {}

    def file_of(self, day: date) -> Path:
        return self.path / f"{day:%Y%m%d}.arrow"

    def days(self) -> list[date]:
        """缓存里有的交易日"""
        days: list[date] = []
        for file in self.path.glob("*.arrow"):
            try:
                days.append(datetime.strptime(file.stem, "%Y%m%d").date())
            except ValueError:
                continue
        return sorted(days)

    def save(self, item: D) -> None:
        """写一天"""
        self.write_file(self.file_of(item.trading_day), item)
        if self.memory:
            self.loaded[item.trading_day] = item

    def load(self, day: date) -> D | None:
        """读一天"""
        if day in self.loaded:
            return self.loaded[day]

        file: Path = self.file_of(day)
        if not file.exists():
            return None

        try:
            item: D = self.read_file(file)
        except (OSError, ValueError, KeyError):
            file.replace(file.with_suffix(".bad"))
            return None
        if self.memory:
            self.loaded[day] = item
        return item

    def write_file(self, file: Path, item: D) -> None:
        """写一个 Arrow 文件"""
        arrays, metadata = self.to_table(item)
        table: pa.Table = pa.table(arrays).replace_schema_metadata({"version": str(self.version), **metadata})

        file.parent.mkdir(parents=True, exist_ok=True)
        temp: Path = file.with_suffix(".tmp")
        options: ipc.IpcWriteOptions = ipc.IpcWriteOptions(compression="zstd")
        with pa.OSFile(str(temp), "wb") as sink, ipc.new_file(sink, table.schema, options=options) as writer:
            writer.write_table(table)
        temp.replace(file)

    def read_file(self, file: Path) -> D:
        """读一个 Arrow 文件"""
        with pa.memory_map(str(file)) as source:
            table: pa.Table = ipc.open_file(source).read_all()
            metadata: dict[str, str] = {key.decode(): value.decode() for key, value in (table.schema.metadata or {}).items()}
            if int(metadata["version"]) != self.version:
                raise ValueError(f"缓存格式版本 {metadata['version']} 与当前版本 {self.version} 不同：{file}")
            return self.from_table(table, metadata)

    @abstractmethod
    def to_table(self, item: D) -> tuple[dict[str, pa.Array], dict[str, str]]:
        """一天转成 Arrow 表"""

    @abstractmethod
    def from_table(self, table: pa.Table, metadata: dict[str, str]) -> D:
        """Arrow 表转回一天"""


@dataclass
class MinuteDay:
    """一个交易日的分钟数据"""

    trading_day: date
    minutes: list[datetime]
    contracts: list[ContractData]
    columns: dict[str, np.ndarray]
    fingerprint: str = ""

    def __post_init__(self) -> None:
        self.slots: list[str] = [c.vt_symbol for c in self.contracts]
        self.slot_index: dict[str, int] = {vt_symbol: i for i, vt_symbol in enumerate(self.slots)}

    @classmethod
    def from_bars(cls, trading_day: date, contracts: list[ContractData], bars: list[BarData]) -> "MinuteDay":
        """由分钟 K 线建一天"""
        slot_index: dict[str, int] = {c.vt_symbol: i for i, c in enumerate(contracts)}
        bars = [bar for bar in bars if bar.vt_symbol in slot_index]
        minutes: list[datetime] = sorted({naive_time(bar.datetime) for bar in bars})
        minute_index: dict[datetime, int] = {minute: i for i, minute in enumerate(minutes)}

        shape: tuple[int, int] = (len(minutes), len(contracts))
        columns: dict[str, np.ndarray] = {name: np.full(shape, np.nan) for name in MINUTE_PRICE_FIELDS}
        columns.update({name: np.zeros(shape) for name in MINUTE_VOLUME_FIELDS})
        columns["flags"] = np.full(shape, CacheFlag.NO_QUOTE, dtype=np.uint8)
        for bar in bars:
            m, slot = minute_index[naive_time(bar.datetime)], slot_index[bar.vt_symbol]
            for name, value in (
                ("open", bar.open_price), ("high", bar.high_price), ("low", bar.low_price),
                ("close", bar.close_price), ("volume", bar.volume), ("turnover", bar.turnover),
                ("open_interest", bar.open_interest),
            ):
                columns[name][m, slot] = value
        return cls(trading_day, minutes, contracts, columns)

    def bar(self, m: int, slot: int) -> BarData:
        """某分钟某槽位的 K 线"""
        contract: ContractData = self.contracts[slot]
        columns: dict[str, np.ndarray] = self.columns
        return BarData(
            symbol=contract.symbol,
            exchange=contract.exchange,
            datetime=self.minutes[m].replace(tzinfo=DB_TZ),
            interval=Interval.MINUTE,
            volume=float(columns["volume"][m, slot]),
            turnover=float(columns["turnover"][m, slot]),
            open_interest=float(columns["open_interest"][m, slot]),
            open_price=float(columns["open"][m, slot]),
            high_price=float(columns["high"][m, slot]),
            low_price=float(columns["low"][m, slot]),
            close_price=float(columns["close"][m, slot]),
            gateway_name="CACHE",
        )

    def traded(self, m: int) -> np.ndarray:
        """第 m 分钟各槽位有没有成交；vnpy 按 tick 最新价合成 K 线，没有成交也会有成交量为 0 的 K 线"""
        return self.columns["volume"][m] > 0

    def has_bar(self, m: int) -> np.ndarray:
        """第 m 分钟各槽位有没有 K 线"""
        return np.isfinite(self.columns["close"][m])

    def bars_of(self, vt_symbol: str, start: datetime, end: datetime) -> list[BarData]:
        """合约在时段内的 K 线"""
        slot: int | None = self.slot_index.get(vt_symbol)
        if slot is None:
            return []
        return [
            self.bar(m, slot) for m, minute in enumerate(self.minutes)
            if np.isfinite(self.columns["close"][m, slot]) and start <= minute.replace(tzinfo=DB_TZ) < end
        ]


class MinuteCache(DayCache[MinuteDay]):
    """分钟缓存仓"""

    def to_table(self, day: MinuteDay) -> tuple[dict[str, pa.Array], dict[str, str]]:
        arrays: dict[str, pa.Array] = {}
        for name, matrix in day.columns.items():
            kind: pa.DataType = _arrow_type(name)
            arrays[name] = pa.array(matrix.reshape(-1).astype(kind.to_pandas_dtype()), type=kind)

        metadata: dict[str, str] = {
            "trading_day": day.trading_day.isoformat(),
            "minutes": json.dumps([minute.isoformat() for minute in day.minutes]),
            "contracts": json.dumps([_contract_to_dict(c) for c in day.contracts], ensure_ascii=False),
            "fingerprint": day.fingerprint,
        }
        return arrays, metadata

    def from_table(self, table: pa.Table, metadata: dict[str, str]) -> MinuteDay:
        contracts: list[ContractData] = [_contract_from_dict(data) for data in json.loads(metadata["contracts"])]
        minutes: list[datetime] = [datetime.fromisoformat(minute) for minute in json.loads(metadata["minutes"])]
        shape: tuple[int, int] = (len(minutes), len(contracts))
        pricetick: np.ndarray = np.array([c.pricetick for c in contracts])

        columns: dict[str, np.ndarray] = {}
        for name in table.column_names:
            matrix: np.ndarray = table.column(name).to_numpy().reshape(shape)
            if name == "flags":
                columns[name] = matrix.astype(np.uint8)
            elif name in MINUTE_PRICE_FIELDS:
                columns[name] = np.round(matrix / pricetick) * pricetick
            else:
                columns[name] = matrix.astype(np.float64)

        return MinuteDay(date.fromisoformat(metadata["trading_day"]), minutes, contracts, columns, metadata["fingerprint"])


@dataclass
class TickDay:
    """一个交易日的 tick"""

    trading_day: date
    contracts: list[ContractData]
    covered: list[str]
    columns: dict[str, np.ndarray]

    def __post_init__(self) -> None:
        self.slot_index: dict[str, int] = {c.vt_symbol: i for i, c in enumerate(self.contracts)}

    @cached_property
    def slot_rows(self) -> tuple[np.ndarray, np.ndarray]:
        """按槽位分组的行号"""
        slot: np.ndarray = self.columns["slot"]
        order: np.ndarray = np.argsort(slot, kind="stable")
        return order, np.searchsorted(slot[order], np.arange(len(self.contracts) + 1))

    @classmethod
    def from_ticks(
        cls, trading_day: date, contracts: list[ContractData], ticks: list[TickData], covered: list[str]
    ) -> "TickDay":
        """由 TickData 建一天"""
        slot_index: dict[str, int] = {c.vt_symbol: i for i, c in enumerate(contracts)}
        ticks = sorted((tick for tick in ticks if tick.vt_symbol in slot_index), key=lambda tick: tick.datetime)
        columns: dict[str, np.ndarray] = {
            "slot": np.array([slot_index[tick.vt_symbol] for tick in ticks], dtype=np.int32),
            "time": np.array([_nanoseconds(tick.datetime) for tick in ticks], dtype=np.int64),
        }
        columns.update({
            name: np.array([float(getattr(tick, field)) for tick in ticks]) for name, field in TICK_COLUMNS.items()
        })
        return cls(trading_day, contracts, sorted({vt_symbol for vt_symbol in covered if vt_symbol in slot_index}), columns)

    def ticks_of(self, vt_symbol: str) -> Sequence[TickData]:
        """某合约当天的 tick"""
        slot: int | None = self.slot_index.get(vt_symbol)
        if slot is None:
            return []
        order, bounds = self.slot_rows
        return TickSeries(self.contracts[slot], order[bounds[slot]:bounds[slot + 1]], self.columns)

    def merge(self, other: "TickDay") -> "TickDay":
        """并入另一份 tick"""
        remap: np.ndarray = np.array([self.slot_index.get(c.vt_symbol, -1) for c in other.contracts], dtype=np.int32)
        slots: np.ndarray = remap[other.columns["slot"]]
        keep: np.ndarray = slots >= 0
        columns: dict[str, np.ndarray] = {
            name: np.concatenate([values, slots[keep] if name == "slot" else other.columns[name][keep]])
            for name, values in self.columns.items()
        }
        order: np.ndarray = np.argsort(columns["time"], kind="stable")
        columns = {name: values[order] for name, values in columns.items()}
        covered: list[str] = sorted({*self.covered, *(s for s in other.covered if s in self.slot_index)})
        return TickDay(self.trading_day, self.contracts, covered, columns)


class TickSeries(Sequence[TickData]):
    """一个合约当天的 tick"""

    fields: tuple[str, ...] = tuple(TICK_COLUMNS.values())

    def __init__(self, contract: ContractData, rows: np.ndarray, columns: dict[str, np.ndarray]) -> None:
        self.symbol: str = contract.symbol
        self.exchange: Exchange = contract.exchange
        self.times: np.ndarray = columns["time"][rows]
        self.values: np.ndarray = np.stack([columns[name][rows] for name in TICK_COLUMNS], axis=1)

    def __len__(self) -> int:
        return len(self.times)

    def __getitem__(self, i: int) -> TickData:  # type: ignore[override]
        return TickData(
            symbol=self.symbol,
            exchange=self.exchange,
            datetime=_from_nanoseconds(int(self.times[i])),
            gateway_name="CACHE",
            **dict(zip(self.fields, self.values[i].tolist(), strict=True)),
        )


class TickCache(DayCache[TickDay]):
    """tick 缓存仓"""

    def to_table(self, day: TickDay) -> tuple[dict[str, pa.Array], dict[str, str]]:
        arrays: dict[str, pa.Array] = {
            "slot": pa.array(day.columns["slot"], type=pa.int32()),
            "time": pa.array(day.columns["time"], type=pa.int64()),
        }
        for name in TICK_COLUMNS:
            kind: pa.DataType = _arrow_type(name)
            arrays[name] = pa.array(day.columns[name].astype(kind.to_pandas_dtype()), type=kind)

        metadata: dict[str, str] = {
            "trading_day": day.trading_day.isoformat(),
            "contracts": json.dumps([_contract_to_dict(c) for c in day.contracts], ensure_ascii=False),
            "covered": json.dumps(day.covered),
        }
        return arrays, metadata

    def from_table(self, table: pa.Table, metadata: dict[str, str]) -> TickDay:
        contracts: list[ContractData] = [_contract_from_dict(data) for data in json.loads(metadata["contracts"])]
        slot: np.ndarray = table.column("slot").to_numpy().astype(np.int32)
        pricetick: np.ndarray = np.array([c.pricetick for c in contracts])[slot]

        columns: dict[str, np.ndarray] = {"slot": slot, "time": table.column("time").to_numpy().astype(np.int64)}
        for name in TICK_COLUMNS:
            values: np.ndarray = table.column(name).to_numpy()
            if name in TICK_PRICE_COLUMNS:
                columns[name] = np.round(values / pricetick) * pricetick
            else:
                columns[name] = values.astype(np.float64)

        return TickDay(date.fromisoformat(metadata["trading_day"]), contracts, json.loads(metadata["covered"]), columns)


class HistoryProvider:
    """历史 K 线数据源"""

    def __init__(
        self, database: BaseDatabase, datafeed: BaseDatafeed, output: Callable[[str], None],
        cache: MinuteCache | None = None,
    ) -> None:
        self.database: BaseDatabase = database
        self.datafeed: BaseDatafeed = datafeed
        self.output: Callable[[str], None] = output
        self.cache: MinuteCache | None = cache
        self.days: dict[date, MinuteDay] = {}
        self.today_bars: dict[str, list[BarData]] = defaultdict(list)

    def load_bar(self, vt_symbol: str, end: datetime, days: int, interval: Interval) -> list[BarData]:
        """取历史 K 线"""
        symbol, exchange = extract_vt_symbol(vt_symbol)
        end = end.astimezone(DB_TZ)
        start: datetime = end - timedelta(days)

        cached: list[BarData] = []
        spans: list[tuple[datetime, datetime]] = []
        if self.cache and interval == Interval.MINUTE:
            cached, spans = self.load_cached(vt_symbol, start, end)

        data: list[BarData] = [
            bar for bar in self.database.load_bar_data(symbol, exchange, interval, start, end)
            if not any(first <= bar.datetime <= last for first, last in spans)
        ]
        if not data and not cached:
            req: HistoryRequest = HistoryRequest(symbol=symbol, exchange=exchange, interval=interval, start=start, end=end)
            data = self.datafeed.query_bar_history(req, self.output) or []
            if data:
                self.database.save_bar_data(data)

        data = sorted(cached + data, key=lambda bar: bar.datetime)

        if interval == Interval.MINUTE:
            last: datetime | None = data[-1].datetime if data else None
            data = data + [bar for bar in self.today_bars.get(vt_symbol, []) if not last or bar.datetime > last]
        return [bar for bar in data if bar.datetime < end]

    def load_cached(
        self, vt_symbol: str, start: datetime, end: datetime
    ) -> tuple[list[BarData], list[tuple[datetime, datetime]]]:
        """从缓存取 K 线"""
        bars: list[BarData] = []
        spans: list[tuple[datetime, datetime]] = []
        for day in self.cache.days():
            if day < start.date():
                continue
            data: MinuteDay | None = self.days.get(day)
            if data is None:
                data = self.cache.load(day)
                if data is None:
                    self.output(f"缓存交易日 {day} 读取失败，文件已改名为 .bad，该日改从数据库取")
                    continue
                self.days[day] = data
            if vt_symbol in data.slot_index:
                spans.append((data.minutes[0].replace(tzinfo=DB_TZ), data.minutes[-1].replace(tzinfo=DB_TZ)))
                bars += data.bars_of(vt_symbol, start, end)
            if day > end.date():
                break
        return bars, spans

    def release(self) -> None:
        """释放缓存交易日"""
        self.days.clear()

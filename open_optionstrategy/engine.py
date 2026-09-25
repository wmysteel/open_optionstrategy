"""策略引擎"""
import glob
import importlib
import json
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from time import monotonic
from types import ModuleType

import numpy as np
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Interval, Offset, OrderType, Product, Status
from vnpy.trader.database import BaseDatabase, get_database
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed
from vnpy.trader.engine import BaseEngine, LogEngine, MainEngine
from vnpy.trader.event import EVENT_ORDER, EVENT_TICK, EVENT_TRADE
from vnpy.trader.object import (
    BarData,
    CancelRequest,
    ContractData,
    LogData,
    OrderData,
    OrderRequest,
    SubscribeRequest,
    TickData,
    TradeData,
)
from vnpy.trader.setting import SETTINGS
from vnpy.trader.utility import get_file_path, load_json, round_to

from .base import APP_NAME, EVENT_OPTION_LOG, EVENT_OPTION_STRATEGY, GREEK_NAMES, INTERVAL_SECONDS, EngineType, StrikeRangeKind
from .manager import ContractManager
from .object import (
    Calendar,
    Clock,
    ExecutionChannel,
    ExecutionSettings,
    MarketProfile,
    MarketState,
    OptionFilter,
    PortfolioData,
    PricingModel,
    Settlement,
    SnapshotSource,
    load_profiles,
    route_profiles,
)
from .template import StrategyTemplate
from .utility import HistoryProvider, MinuteCache, OptionBarGenerator, TickSnapshotSource, default_cache_path

SAVE_LOCK: Lock = Lock()


def save_json(filename: str, data: dict) -> None:
    """原子写入 JSON 文件"""
    filepath: Path = get_file_path(filename)
    temp: Path = filepath.with_suffix(filepath.suffix + ".tmp")
    with SAVE_LOCK:
        with open(temp, mode="w", encoding="UTF-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        temp.replace(filepath)


def _theo(model: PricingModel, f: np.ndarray, k: np.ndarray, t: np.ndarray, r: float, v: np.ndarray, cp: np.ndarray) -> np.ndarray:
    """理论价：到期（t 为 0）取内在价值，波动率不大于 0 为 NaN"""
    with np.errstate(all="ignore"):
        value = model.price(f, k, t, r, np.where(v > 0, v, np.nan), cp)
    return np.where(t > 0, value, np.maximum(cp * (f - k), 0.0))


class WallClock:
    """实盘时钟"""

    def now(self) -> datetime:
        return datetime.now()


class GatewayChannel:
    """vnpy 网关执行通道"""

    def __init__(self, main_engine: MainEngine) -> None:
        self.main_engine = main_engine

    def convert_order_request(self, req: OrderRequest, gateway_name: str) -> list[OrderRequest]:
        return self.main_engine.convert_order_request(req, gateway_name, False, False)

    def send_order(self, req: OrderRequest, gateway_name: str) -> str:
        vt_orderid: str = self.main_engine.send_order(req, gateway_name)
        if vt_orderid:
            self.main_engine.update_order_request(req, vt_orderid, gateway_name)
        return vt_orderid

    def cancel_order(self, req: CancelRequest, gateway_name: str) -> None:
        self.main_engine.cancel_order(req, gateway_name)

    def available(self, gateway_name: str) -> float | None:
        """账户可用资金"""
        for account in self.main_engine.get_all_accounts():
            if account.gateway_name == gateway_name:
                return account.available
        return None


@dataclass
class StrategyBook:
    """策略账本"""

    positions: np.ndarray
    portfolios: dict[str, PortfolioData]
    active_orderids: set[str] = field(default_factory=set)
    targets: dict[str, int] = field(default_factory=dict)
    combos: dict[str, tuple[dict[str, int], int]] = field(default_factory=dict)
    scopes: list[tuple[str, OptionFilter]] = field(default_factory=list)
    watch_list: set[str] = field(default_factory=set)
    watched: set[str] = field(default_factory=set)
    watch_key: tuple = ()
    resend_at: dict[str, float] = field(default_factory=dict)
    last_reject: dict[str, str] = field(default_factory=dict)
    dropped_positions: dict[str, int] = field(default_factory=dict)
    costs: dict[str, float] = field(default_factory=dict)

    cash_today: float = 0.0
    positions_open: np.ndarray | None = None
    open_mark: np.ndarray = field(init=False)
    pnl_today: float = 0.0
    margin: float = 0.0
    expiry_notified: bool = False
    warned_targets: set[str] = field(default_factory=set)
    rejects: dict[str, int] = field(default_factory=dict)
    restored: bool = False

    def __post_init__(self) -> None:
        self.open_mark = np.full(len(self.positions), np.nan)

    def new_day(self, positions: np.ndarray, portfolios: dict[str, PortfolioData]) -> None:
        """换日"""
        for portfolio in self.portfolios.values():
            portfolio.expired = True
        self.positions, self.portfolios = positions, portfolios
        self.watched, self.watch_key = set(), ()
        self.cash_today, self.positions_open, self.pnl_today, self.margin = 0.0, None, 0.0, 0.0
        self.open_mark = np.full(len(positions), np.nan)
        self.expiry_notified = False
        self.warned_targets = set()
        self.rejects = {}


class StrategyEngineBase:
    """实盘与回测共用的策略引擎"""

    clock: Clock
    source: SnapshotSource
    channel: ExecutionChannel
    history: HistoryProvider
    engine_type: EngineType

    def __init__(self) -> None:
        self.risk_free_rate: float = float(SETTINGS.get("optionstrategy.risk_free_rate", 0.02))
        execution: dict = SETTINGS.get("optionstrategy.execution", {})
        unknown: set[str] = set(execution) - {f.name for f in fields(ExecutionSettings)}
        if unknown:
            raise ValueError("vt_setting.json 的 optionstrategy.execution 有不认识的键：" + "、".join(sorted(unknown)))
        self.execution: ExecutionSettings = ExecutionSettings(**execution)
        self.rate_table: dict[date, float] = {}
        self.stale_seconds: float = 300.0
        self.max_spread: float = 1.0
        self.profiles: list[MarketProfile] = []
        self.strategies: dict[str, StrategyTemplate] = {}
        self.clear_state()

    def clear_state(self) -> None:
        """清空当日状态与账本"""
        self._rate: float = self.risk_free_rate
        self.state: MarketState = MarketState()
        self.manager: ContractManager | None = None
        self._last_seq: int = -1
        self._position_slots: list[str] = []
        self._last_mark: np.ndarray = np.empty(0)
        self._last_long_margin: np.ndarray = np.empty(0)
        self._last_short_margin: np.ndarray = np.empty(0)
        self._plugin_failed: set[str] = set()
        self.books: dict[str, StrategyBook] = {}
        self.strategy_errors: dict[str, str] = {}
        self.orders: dict[str, OrderData] = {}
        self.order_requests: dict[str, OrderRequest] = {}
        self.order_owner: dict[str, str] = {}
        self._cancelling: dict[str, float] = {}
        self._booked: dict[str, dict[str, int]] = {}
        self._warned: set[tuple[str, str, str]] = set()
        self.margin_occupied: float = 0.0
        self.position_value: float = 0.0

    def write_log(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """写日志"""
        raise NotImplementedError("由实盘引擎与回测引擎实现")

    def put_strategy_event(self, strategy: StrategyTemplate) -> None:
        """推送策略数据"""
        raise NotImplementedError("由实盘引擎与回测引擎实现")

    def send_notification(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """通过已配置渠道推送通知"""
        raise NotImplementedError("由实盘引擎与回测引擎实现")

    def sync_strategy_data(self, strategy: StrategyTemplate) -> None:
        """保存策略数据"""
        raise NotImplementedError("由实盘引擎与回测引擎实现")

    def save_strategy_file(self, file_name: str, data: dict) -> None:
        """保存策略自定义数据"""
        raise NotImplementedError("由实盘引擎与回测引擎实现")

    def load_strategy_file(self, file_name: str) -> dict:
        """读取策略自定义数据"""
        raise NotImplementedError("由实盘引擎与回测引擎实现")

    def load_profiles(self) -> None:
        """加载规则实现"""
        names: list[str] = [n.strip() for n in SETTINGS.get("optionstrategy.profiles", "").split(",") if n.strip()]
        try:
            self.profiles = load_profiles(names)
        except Exception:
            self.write_log(f"规则实现加载失败，触发异常：\n{traceback.format_exc()}")
            return
        self.write_log("已加载规则实现：{}".format("、".join(p.name for p in self.profiles) or "无"))

    def get_engine_type(self) -> EngineType:
        """获取引擎类型"""
        return self.engine_type

    def register_strategy(self, strategy: StrategyTemplate) -> None:
        """登记策略"""
        if self.manager is None:
            raise RuntimeError("先调用 start_day 再登记策略")
        positions = np.zeros(len(self.manager.slots), dtype=np.int64)
        self.strategies[strategy.strategy_name] = strategy
        self.books[strategy.strategy_name] = StrategyBook(positions, self.manager.build_portfolios(self.state, positions))

    def call_strategy_func(self, strategy: StrategyTemplate, func: Callable, *args: object) -> None:
        """安全调用策略函数"""
        try:
            func(*args)
        except Exception:
            strategy.trading = False
            strategy.inited = False
            msg: str = f"触发异常已停止\n{traceback.format_exc()}"
            self.write_log(msg, strategy)
            self.strategy_errors[strategy.strategy_name] = msg
            self.cancel_all(strategy)
            try:
                self.sync_strategy_data(strategy)
            except ValueError as e:
                self.write_log(str(e), strategy)
            self.put_strategy_event(strategy)

    def start_day(self, contracts: list[ContractData], trading_day: date) -> None:
        """建当日状态"""
        manager = ContractManager(contracts, self.profiles)
        self._last_seq = -1
        self.manager = manager
        self.state.trading_day = trading_day
        effective = [d for d in self.rate_table if d <= trading_day]
        self._rate = self.rate_table[max(effective)] if effective else self.risk_free_rate
        self.state.snapshot = None
        self.state.slot_arrays = {}
        self.state.chain_arrays = {}
        self._last_mark = np.full(len(contracts), np.nan)
        self._last_long_margin = np.full(len(contracts), np.nan)
        self._last_short_margin = np.full(len(contracts), np.nan)
        for name, book in self.books.items():
            positions = self._remap_positions(name, book.positions, manager)
            book.new_day(positions, manager.build_portfolios(self.state, positions))
        self._position_slots = manager.slots
        self._warned.clear()
        self._plugin_failed.clear()

    def step(self) -> bool:
        """处理最新快照"""
        snapshot = self.source.latest()
        if snapshot is None or snapshot.seq == self._last_seq or self.manager is None:
            return False
        if snapshot.slot_count != len(self.manager.slots):
            raise ValueError(f"快照槽位数 {snapshot.slot_count} 与槽位表 {len(self.manager.slots)} 不一致")
        self._last_seq = snapshot.seq
        now = self.clock.now()
        slot_arrays, chain_arrays = self.manager.compute(
            snapshot, now.timestamp(), self._rate, self.stale_seconds, self.max_spread
        )
        self.state.slot_arrays = slot_arrays
        self._update_account(slot_arrays["mark"], slot_arrays["long_margin"], slot_arrays["short_margin"])
        self.state.chain_arrays = chain_arrays
        self.state.snapshot = snapshot
        for name, book in list(self.books.items()):
            strategy = self.strategies[name]
            if not strategy.inited:
                continue
            if strategy.trading and not book.expiry_notified:
                book.expiry_notified = True
                self._notify_expiry(strategy, book)
            self.call_strategy_func(strategy, strategy.on_snapshot, snapshot)
        return True

    def _notify_expiry(self, strategy: StrategyTemplate, book: StrategyBook) -> None:
        """到期提醒"""
        manager: ContractManager = self.manager
        expiring = [
            book.portfolios[manager.chain_keys[manager.chain_of_slot[slot]][0]].options[manager.slots[slot]]
            for slot in self._expiring_slots(book)
        ]
        if not expiring:
            return
        self.write_log(f"今日到期：{strategy.strategy_name}：" + "、".join(f"{o.vt_symbol} {o.pos} 手" for o in expiring))
        self.call_strategy_func(strategy, strategy.on_expiry, expiring)

    def _expiring_slots(self, book: StrategyBook) -> list[int]:
        """今天到期的持仓期权槽位"""
        manager: ContractManager = self.manager
        today = self.state.trading_day
        return [
            int(slot) for slot in np.flatnonzero(book.positions)
            if (attributes := manager.attributes.get(manager.slots[slot])) is not None and attributes.last_exercise_date == today
        ]

    def _update_account(self, mark: np.ndarray, long_margin: np.ndarray, short_margin: np.ndarray) -> None:
        """更新账户与当日盈亏"""
        valid = np.isfinite(mark)
        self._last_mark[valid] = mark[valid]
        for last, now in ((self._last_long_margin, long_margin), (self._last_short_margin, short_margin)):   # 算不出的沿用当日上一次的值
            known = np.isfinite(now)
            last[known] = now[known]
        size = self.manager.size
        occupied = 0.0
        total = 0.0
        for book in self.books.values():
            if book.positions_open is None:
                book.positions_open = book.positions.copy()
            first = valid & np.isnan(book.open_mark)
            book.open_mark[first] = mark[first]
            held = np.flatnonzero((book.positions != 0) | (book.positions_open != 0))
            pos, pos_open, held_size = book.positions[held], book.positions_open[held], size[held]
            book.margin = float(np.sum(np.where(
                pos > 0, pos * np.nan_to_num(self._last_long_margin[held]), -pos * np.nan_to_num(self._last_short_margin[held])
            )))
            occupied += book.margin
            v = float(np.dot(pos, np.nan_to_num(self._last_mark[held]) * held_size))
            total += v
            book.pnl_today = book.cash_today + v - float(np.dot(pos_open, np.nan_to_num(book.open_mark[held]) * held_size))
        self.margin_occupied = occupied
        self.position_value = total

    def get_portfolio(self, strategy: StrategyTemplate, portfolio_name: str) -> PortfolioData | None:
        """查询期权组合视图"""
        book = self.books.get(strategy.strategy_name)
        return book.portfolios.get(portfolio_name) if book else None

    def get_pos(self, strategy: StrategyTemplate, vt_symbol: str) -> int:
        """查询持仓"""
        slot = self.manager.slot_index.get(vt_symbol)
        return 0 if slot is None else int(self.books[strategy.strategy_name].positions[slot])

    def get_price(self, strategy: StrategyTemplate, vt_symbol: str) -> float:
        """合约当日最近一次有效的盯市价"""
        slot = self.manager.slot_index.get(vt_symbol)
        return float("nan") if slot is None else float(self._last_mark[slot])

    def get_greeks(self, strategy: StrategyTemplate, portfolio_name: str = "") -> dict[str, float]:
        """持仓 Greeks 合计"""
        if portfolio_name:
            portfolio: PortfolioData | None = self.get_portfolio(strategy, portfolio_name)
            return portfolio.greeks() if portfolio else {}
        pos = self.books[strategy.strategy_name].positions
        held = np.flatnonzero(pos)
        return {name: float(np.dot(pos[held], array[held])) if (array := self.state.slot_arrays.get(name)) is not None else float("nan")
                for name in GREEK_NAMES}

    def _leg_slots(self, legs: dict[str, int]) -> tuple[np.ndarray, np.ndarray] | None:
        """一组腿的槽位与比例；有一条腿不在当日主档时为 None"""
        slot_index = self.manager.slot_index
        if any(vt_symbol not in slot_index for vt_symbol in legs):
            return None
        return np.array([slot_index[vt_symbol] for vt_symbol in legs], dtype=np.int64), np.array(list(legs.values()), dtype=float)

    def get_combo_greeks(self, strategy: StrategyTemplate, legs: dict[str, int]) -> dict[str, float]:
        """一组腿按比例（带方向）的希腊值合计；有一条腿不在当日主档或希腊值无效时该项为 NaN"""
        found = self._leg_slots(legs)
        if found is None:
            return {name: float("nan") for name in GREEK_NAMES}
        slots, ratios = found
        return {name: float(np.dot(ratios, array[slots])) if (array := self.state.slot_arrays.get(name)) is not None else float("nan")
                for name in GREEK_NAMES}

    def get_combo_premium(self, strategy: StrategyTemplate, legs: dict[str, int], cross: bool = False) -> float:
        """一组腿按比例（带方向）的净权利金（元），正数为净支付；cross 为真时买腿取卖价、卖腿取买价，否则按中间价"""
        found = self._leg_slots(legs)
        snapshot = self.state.snapshot
        if found is None or snapshot is None:
            return float("nan")
        slots, ratios = found
        bid, ask = snapshot.columns["bid1"][slots], snapshot.columns["ask1"][slots]
        prices = np.where(ratios > 0, ask, bid) if cross else (bid + ask) / 2
        return float(np.dot(ratios, prices * self.manager.size[slots]))

    def get_theo_price(
        self, strategy: StrategyTemplate, vt_symbol: str, vol: float, underlying: float | None = None, days: int = 0
    ) -> float:
        """按期权自己的定价模型、给定波动率的理论价（不乘合约乘数）；不给标的价用当前的，days 为假设过了几个自然日；
        不是期权、不在当日主档或当日还没行情时为 NaN"""
        manager: ContractManager = self.manager
        slot: int | None = manager.slot_index.get(vt_symbol)
        model: PricingModel | None = manager.model_of.get(slot) if slot is not None else None
        arrays = self.state.slot_arrays
        if slot is None or model is None or "tte" not in arrays:
            return float("nan")
        f = arrays["underlying"][slot] if underlying is None else underlying
        t = max(float(arrays["tte"][slot]) - days / 365, 0.0)
        return float(_theo(model, np.array([f]), manager.strike[[slot]], np.array([t]), self._rate, np.array([vol]), manager.cp[[slot]])[0])

    def get_combo_margin(self, strategy: StrategyTemplate, legs: dict[str, int]) -> float:
        """一组腿的保证金预估（元）：买的腿按每手多头保证金、卖的腿按每手空头保证金，不含交易所的组合保证金优惠；
        有一条腿不在当日主档或当日还没算出过保证金时为 NaN"""
        found = self._leg_slots(legs)
        if found is None:
            return float("nan")
        slots, ratios = found
        per_lot = np.where(ratios > 0, self._last_long_margin[slots], self._last_short_margin[slots])
        return float(np.dot(np.abs(ratios), per_lot))

    def get_pnl_today(self, strategy: StrategyTemplate) -> float:
        """本策略当日盈亏"""
        return self.books[strategy.strategy_name].pnl_today

    def get_margin(self, strategy: StrategyTemplate) -> float:
        """本策略保证金占用"""
        return self.books[strategy.strategy_name].margin

    def get_available(self, strategy: StrategyTemplate) -> float | None:
        """账户可用资金"""
        return self.channel.available(strategy.gateway_name)

    def get_pos_data(self, strategy: StrategyTemplate) -> dict[str, int]:
        """非零持仓"""
        pos = self.books[strategy.strategy_name].positions
        return {self.manager.slots[slot]: int(pos[slot]) for slot in np.flatnonzero(pos)}

    def load_pos_data(self, strategy: StrategyTemplate, pos_data: dict[str, int]) -> None:
        """恢复持仓"""
        pos = self.books[strategy.strategy_name].positions
        for vt_symbol, volume in pos_data.items():
            slot = self.manager.slot_index.get(vt_symbol)
            if slot is None:
                self._drop_position(strategy.strategy_name, vt_symbol, int(volume))
            else:
                pos[slot] = int(volume)

    def _remap_positions(self, name: str, old: np.ndarray, manager: ContractManager) -> np.ndarray:
        """持仓搬到新槽位"""
        new = np.zeros(len(manager.slots), dtype=np.int64)
        for slot in np.flatnonzero(old):
            vt_symbol = self._position_slots[slot]
            target = manager.slot_index.get(vt_symbol)
            if target is None:
                self._drop_position(name, vt_symbol, int(old[slot]))
            else:
                new[target] = old[slot]
        return new

    def _drop_position(self, name: str, vt_symbol: str, volume: int) -> None:
        """记下未能入账的持仓"""
        dropped = self.books[name].dropped_positions
        dropped[vt_symbol] = dropped.get(vt_symbol, 0) + volume
        self.write_log(f"持仓合约不在当日主档：{name} {vt_symbol} {volume} 手未入账，请人工核对")

    def _plugin_error(self, profile: MarketProfile) -> None:
        """记录规则实现出错"""
        if profile.name in self._plugin_failed:
            return
        self._plugin_failed.add(profile.name)
        self.write_log(f"规则实现出错：{profile.name}：{traceback.format_exc().strip()}")

    def send_order(
        self, strategy: StrategyTemplate, vt_symbol: str, direction: Direction, offset: Offset, price: float, volume: float
    ) -> list[str]:
        """发送限价单"""
        contract: ContractData | None = self.manager.contracts.get(vt_symbol)
        if contract is None:
            self.write_log(f"委托失败，找不到合约：{vt_symbol}", strategy)
            return []
        name, gateway = strategy.strategy_name, strategy.gateway_name
        book = self.books[name]
        volume = int(volume)
        if np.isfinite(price) and price > 0:
            price = round_to(price, contract.pricetick)
        reason = self._reject_reason(strategy, vt_symbol, direction, offset, price, volume)
        if reason:
            self._reject(strategy, vt_symbol, reason)
            return []
        vt_orderids: list[str] = []
        for part in self._split_volume(vt_symbol, volume):
            original_req = OrderRequest(
                symbol=contract.symbol, exchange=contract.exchange, direction=direction, type=OrderType.LIMIT,
                volume=part, price=price, offset=offset, reference=f"{APP_NAME}_{name}",
            )
            reqs: list[OrderRequest] = self.channel.convert_order_request(original_req, gateway)
            if not reqs:
                self._reject(strategy, vt_symbol, "开平转换后没有可发的委托：柜台可平量不足")
                return vt_orderids
            for req in reqs:
                vt_orderid = self.channel.send_order(req, gateway)
                if not vt_orderid:
                    continue
                vt_orderids.append(vt_orderid)
                self.order_owner[vt_orderid] = name
                self.order_requests[vt_orderid] = req
                book.active_orderids.add(vt_orderid)
                self.sync_strategy_data(strategy)
        book.last_reject.pop(vt_symbol, None)
        return vt_orderids

    def _reject(self, strategy: StrategyTemplate, vt_symbol: str, reason: str) -> None:
        """提醒委托被拒"""
        book = self.books[strategy.strategy_name]
        if book.last_reject.get(vt_symbol) != reason:
            book.last_reject[vt_symbol] = reason
            self.write_log(f"委托被拒绝：{strategy.strategy_name} {vt_symbol}：{reason}")

    def _reject_reason(
        self, strategy: StrategyTemplate, vt_symbol: str, direction: Direction, offset: Offset, price: float, volume: int
    ) -> str:
        """委托拒绝原因"""
        book = self.books[strategy.strategy_name]
        if not (np.isfinite(price) and price > 0):
            return f"价格 {price} 无效"
        if volume <= 0:
            return f"数量 {volume} 无效"
        if offset != Offset.OPEN:
            closable = self._closable(book, vt_symbol, direction)
            return f"平仓 {volume} 手超过可平持仓 {closable} 手" if volume > closable else ""
        attributes = self.manager.attributes.get(vt_symbol)
        if attributes and volume < attributes.min_open_volume:
            return f"开仓 {volume} 手小于最小开仓量 {attributes.min_open_volume}"
        return ""

    def _in_flight(self, book: StrategyBook, vt_symbol: str, direction: Direction) -> tuple[int, int]:
        """同方向在途委托量"""
        opening = closing = 0
        for vt_orderid in book.active_orderids:
            req = self.order_requests[vt_orderid]
            if req.vt_symbol == vt_symbol and req.direction == direction:
                if req.offset == Offset.OPEN:
                    opening += self._unbooked(vt_orderid)
                else:
                    closing += self._unbooked(vt_orderid)
        return opening, closing

    def _closable(self, book: StrategyBook, vt_symbol: str, direction: Direction) -> int:
        """可平量"""
        pos = int(book.positions[self.manager.slot_index[vt_symbol]])
        held = max(pos, 0) if direction == Direction.SHORT else max(-pos, 0)
        return held - self._in_flight(book, vt_symbol, direction)[1]

    def _split_volume(self, vt_symbol: str, volume: int) -> list[int]:
        """拆单"""
        attributes = self.manager.attributes.get(vt_symbol)
        cap = attributes.max_order_volume if attributes else 0
        chunk = cap if cap > 0 else volume
        return [chunk] * (volume // chunk) + ([volume % chunk] if volume % chunk else [])

    def adopt_order(self, strategy: StrategyTemplate, order: OrderData, booked: dict[str, int]) -> None:
        """认领重启前的委托"""
        req = OrderRequest(
            symbol=order.symbol, exchange=order.exchange, direction=order.direction, type=order.type, volume=order.volume,
            price=order.price, offset=order.offset, reference=order.reference,
        )
        self.order_owner[order.vt_orderid] = strategy.strategy_name
        self.order_requests[order.vt_orderid] = req
        self.orders[order.vt_orderid] = order
        self._booked[order.vt_orderid] = booked
        book = self.books[strategy.strategy_name]
        book.active_orderids.add(order.vt_orderid)
        self._retire_if_settled(book, order.vt_orderid)

    def process_order(self, order: OrderData) -> None:
        """委托回报"""
        owner = self.order_owner.get(order.vt_orderid)
        if owner is None:
            return
        old = self.orders.get(order.vt_orderid)
        if old is not None and (order.traded < old.traded or (order.is_active() and not old.is_active())):
            return
        strategy = self.strategies[owner]
        self.orders[order.vt_orderid] = order
        book = self.books[owner]
        self._retire_if_settled(book, order.vt_orderid)
        if order.status == Status.REJECTED:
            rejects = book.rejects[order.vt_symbol] = book.rejects.get(order.vt_symbol, 0) + 1
            book.resend_at[order.vt_symbol] = self.clock.now().timestamp() + self.execution.min_reprice_seconds * 2 ** (rejects - 1)
            if ("柜台拒单", strategy.gateway_name, order.vt_symbol) not in self._warned:
                self._warned.add(("柜台拒单", strategy.gateway_name, order.vt_symbol))
                self.write_log(f"委托被柜台拒绝：{owner} {order.vt_symbol}：原因见交易接口日志，当日同一合约只提醒一次，再被拒时重发间隔逐次加倍")
            self._cancel_combo_siblings(strategy, order.vt_symbol)
        if strategy.inited:
            self.call_strategy_func(strategy, strategy.update_order, order)

    def _retire_if_settled(self, book: StrategyBook, vt_orderid: str) -> None:
        """移出已了结的委托"""
        booked = sum(self._booked.get(vt_orderid, {}).values())
        order = self.orders.get(vt_orderid)
        done = order is not None and not order.is_active() and booked >= order.traded
        if done or booked >= self.order_requests[vt_orderid].volume:
            book.active_orderids.discard(vt_orderid)
            self._cancelling.pop(vt_orderid, None)

    def _cancel_combo_siblings(self, strategy: StrategyTemplate, vt_symbol: str) -> None:
        """撤组合其他腿"""
        book = self.books[strategy.strategy_name]
        for combo_name, (legs, _) in book.combos.items():
            if vt_symbol not in legs:
                continue
            others = [i for i in list(book.active_orderids) if self.order_requests[i].vt_symbol in legs]
            for vt_orderid in others:
                self.cancel_order(strategy, vt_orderid)
            for leg in legs:
                book.resend_at[leg] = max(book.resend_at.get(leg, 0.0), book.resend_at[vt_symbol])
            self.write_log(f"组合腿被拒绝：{strategy.strategy_name} {combo_name}：{vt_symbol} 被拒，已撤其他腿 {len(others)} 笔")

    def cancel_order(self, strategy: StrategyTemplate, vt_orderid: str) -> bool:
        """撤销委托"""
        name, gateway = strategy.strategy_name, strategy.gateway_name
        if vt_orderid not in self.books[name].active_orderids:
            return False
        order = self.orders.get(vt_orderid)
        if order is not None and not order.is_active():
            return False
        now_ts = self.clock.now().timestamp()
        if now_ts - self._cancelling.get(vt_orderid, -np.inf) < self.execution.cancel_retry_seconds:
            return False
        req = self.order_requests[vt_orderid]
        self.channel.cancel_order(
            CancelRequest(orderid=vt_orderid.split(".", 1)[1], symbol=req.symbol, exchange=req.exchange), gateway
        )
        self._cancelling[vt_orderid] = now_ts
        return True

    def cancel_all(self, strategy: StrategyTemplate) -> None:
        """全撤本策略的活动委托"""
        for vt_orderid in list(self.books[strategy.strategy_name].active_orderids):
            self.cancel_order(strategy, vt_orderid)

    def process_trade(self, trade: TradeData) -> None:
        """成交记账"""
        owner = self.order_owner.get(trade.vt_orderid)
        slot = self.manager.slot_index.get(trade.vt_symbol) if owner is not None else None
        if slot is None:
            return
        booked = self._booked.setdefault(trade.vt_orderid, {})
        if trade.vt_tradeid in booked:
            return
        booked[trade.vt_tradeid] = int(trade.volume)
        book = self.books[owner]
        sign = 1 if trade.direction == Direction.LONG else -1
        self._move_position(book, slot, sign * int(trade.volume), trade.price)
        book.cash_today -= sign * trade.price * float(self.manager.size[slot]) * trade.volume
        self._retire_if_settled(book, trade.vt_orderid)
        strategy = self.strategies[owner]
        if strategy.inited:
            self.call_strategy_func(strategy, strategy.update_trade, trade)
        self.sync_strategy_data(strategy)

    def _move_position(self, book: StrategyBook, slot: int, change: int, price: float) -> None:
        """持仓变动并更新开仓均价：加仓按成交量加权，减仓不变，翻向取这笔的价格，平完删掉；成本未知的持仓加仓后仍未知"""
        vt_symbol = self.manager.slots[slot]
        before = int(book.positions[slot])
        after = before + change
        book.positions[slot] = after
        if after == 0:
            book.costs.pop(vt_symbol, None)
        elif before == 0 or before * after < 0:
            book.costs[vt_symbol] = price
        elif abs(after) > abs(before) and vt_symbol in book.costs:
            book.costs[vt_symbol] = (book.costs[vt_symbol] * abs(before) + price * abs(change)) / abs(after)

    def get_cost(self, strategy: StrategyTemplate, vt_symbol: str) -> float:
        """本策略该合约持仓的开仓均价；空仓或没有成本记录时为 NaN"""
        return self.books[strategy.strategy_name].costs.get(vt_symbol, float("nan"))

    def get_open_pnl(self, strategy: StrategyTemplate, vt_symbols: list[str] | None = None) -> float:
        """本策略这些合约持仓的浮动盈亏（元）= 持仓 ×（最新盯市价 − 开仓均价）× 乘数；不给合约就算全部持仓；
        有持仓却没有成本或价格时为 NaN"""
        book = self.books[strategy.strategy_name]
        manager: ContractManager = self.manager
        held = [manager.slots[slot] for slot in np.flatnonzero(book.positions)] if vt_symbols is None else vt_symbols
        total = 0.0
        for vt_symbol in held:
            slot = manager.slot_index.get(vt_symbol)
            if slot is None:
                return float("nan")
            if book.positions[slot]:
                total += book.positions[slot] * (self._last_mark[slot] - book.costs.get(vt_symbol, np.nan)) * manager.size[slot]
        return float(total)

    def get_order(self, vt_orderid: str) -> OrderData | None:
        """查询委托"""
        return self.orders.get(vt_orderid, None)

    def get_all_active_orderids(self, strategy: StrategyTemplate) -> list[str]:
        """查询活动委托号"""
        book = self.books.get(strategy.strategy_name)
        return list(book.active_orderids) if book else []

    def set_target(self, strategy: StrategyTemplate, vt_symbol: str, target: int) -> None:
        """设置单合约目标仓位"""
        book = self.books[strategy.strategy_name]
        combo = self._combo_of(book, vt_symbol)
        if combo:
            raise ValueError(f"{vt_symbol} 是组合 {combo} 的腿，不能再设单合约目标")
        book.targets[vt_symbol] = int(target)

    def get_target(self, strategy: StrategyTemplate, vt_symbol: str) -> int:
        return self.books[strategy.strategy_name].targets.get(vt_symbol, 0)

    def set_combo_target(self, strategy: StrategyTemplate, name: str, legs: dict[str, int], target: int) -> None:
        """设置组合目标"""
        book = self.books[strategy.strategy_name]
        for vt_symbol in legs:
            if vt_symbol in book.targets:
                raise ValueError(f"{vt_symbol} 已有单合约目标，不能再作组合 {name} 的腿")
            other = self._combo_of(book, vt_symbol, exclude=name)
            if other:
                raise ValueError(f"{vt_symbol} 已是组合 {other} 的腿，不能再作组合 {name} 的腿")
        book.combos[name] = ({vt_symbol: int(ratio) for vt_symbol, ratio in legs.items()}, int(target))

    @staticmethod
    def _combo_of(book: StrategyBook, vt_symbol: str, exclude: str = "") -> str:
        """合约所在的组合"""
        return next((name for name, (legs, _) in book.combos.items() if name != exclude and vt_symbol in legs), "")

    def get_combo_target(self, strategy: StrategyTemplate, name: str) -> int:
        combo = self.books[strategy.strategy_name].combos.get(name)
        return combo[1] if combo else 0

    def get_combo_pos(self, strategy: StrategyTemplate, name: str) -> int:
        """组合已建成的份数：各腿持仓除以比例，取离 0 最近的一条腿；各腿方向不一致或没有这个组合时为 0"""
        combo = self.books[strategy.strategy_name].combos.get(name)
        if combo is None:
            return 0
        units = [self.get_pos(strategy, vt_symbol) / ratio for vt_symbol, ratio in combo[0].items()]
        nearest = min(units, key=abs)
        return int(nearest) if all(u * nearest > 0 for u in units) else 0

    def clear_targets(self, strategy: StrategyTemplate) -> int:
        """清空目标仓位"""
        book = self.books[strategy.strategy_name]
        count = len(book.targets) + len(book.combos)
        book.targets.clear()
        book.combos.clear()
        return count

    def _spread(self, vt_symbol: str) -> float:
        """相对价差"""
        snapshot = self.state.snapshot
        if snapshot is None:
            return float("inf")
        slot = self.manager.slot_index[vt_symbol]
        bid, ask = float(snapshot.columns["bid1"][slot]), float(snapshot.columns["ask1"][slot])
        return (ask - bid) / ((ask + bid) / 2) if np.isfinite(bid) and np.isfinite(ask) and bid + ask > 0 else float("inf")

    def _combo_leg_targets(self, strategy: StrategyTemplate, legs: dict[str, int], target: int) -> dict[str, int]:
        """组合各腿本轮目标仓位"""
        pending: set[str] = {self.order_requests[i].vt_symbol for i in self.books[strategy.strategy_name].active_orderids}
        order = sorted(legs, key=lambda s: (s in pending, self._spread(s)), reverse=True)
        harder: list[float] = []
        result: dict[str, int] = {}
        for vt_symbol in order:
            ratio = legs[vt_symbol]
            units = self.get_pos(strategy, vt_symbol) / ratio
            if not harder:
                allowed = float(target)
            elif target >= units:
                allowed = max(units, min(target, min(harder)))
            else:
                allowed = min(units, max(target, max(harder)))
            result[vt_symbol] = int(round(allowed * ratio))
            harder.append(units)
        return result

    def execute_trading(self, strategy: StrategyTemplate, price_data: dict[str, float], percent_add: float) -> list[str]:
        """按目标仓位调仓"""
        name: str = strategy.strategy_name
        book = self.books[name]
        sent: list[str] = []
        now_ts = self.clock.now().timestamp()
        targets = dict(book.targets)
        for combo_name, (legs, combo_target) in book.combos.items():
            missing = [vt_symbol for vt_symbol in legs if vt_symbol not in self.manager.contracts]
            if missing:
                for vt_symbol in missing:
                    if vt_symbol not in book.warned_targets:
                        book.warned_targets.add(vt_symbol)
                        self.write_log(f"组合目标的腿不在当日主档：{name} {combo_name}：{vt_symbol}：该组合未处理")
                continue
            targets.update(self._combo_leg_targets(strategy, legs, combo_target))
        for vt_symbol, target in targets.items():
            contract = self.manager.contracts.get(vt_symbol)
            if contract is None:
                if vt_symbol not in book.warned_targets:
                    book.warned_targets.add(vt_symbol)
                    self.write_log(f"目标合约不在当日主档：{name} {vt_symbol}：目标 {target} 手未处理")
                continue
            diff = target - self.get_pos(strategy, vt_symbol)
            active = [i for i in book.active_orderids if self.order_requests[i].vt_symbol == vt_symbol]
            if diff == 0:
                if any([self.cancel_order(strategy, vt_orderid) for vt_orderid in active]):
                    book.resend_at[vt_symbol] = now_ts + self.execution.min_reprice_seconds
                continue
            direction = Direction.LONG if diff > 0 else Direction.SHORT
            pricetick = contract.pricetick
            order_price = self._order_price(vt_symbol, price_data.get(vt_symbol, float("nan")), diff > 0, percent_add, pricetick)
            volume = abs(diff)
            if active:
                remaining = self._remaining_if_fit(active, direction, order_price, pricetick)
                if remaining is None or remaining > volume:
                    cancelled = [self.cancel_order(strategy, vt_orderid) for vt_orderid in active]
                    if any(cancelled):
                        book.resend_at[vt_symbol] = now_ts + self.execution.min_reprice_seconds
                    continue
                volume -= remaining
            if volume == 0 or not np.isfinite(order_price) or now_ts < book.resend_at.get(vt_symbol, 0.0):
                continue
            sent += self._send_to_target(strategy, vt_symbol, direction, order_price, volume)
        return sent

    def _order_price(self, vt_symbol: str, price: float, buy: bool, percent_add: float, pricetick: float) -> float:
        """超价、按价位取整并截在涨跌停之内的委托价；没有价格时为 NaN"""
        if not np.isfinite(price):
            return float("nan")
        order_price = round_to(price * (1 + percent_add) if buy else price * (1 - percent_add), pricetick)
        snapshot = self.state.snapshot
        if snapshot is not None:
            slot = self.manager.slot_index[vt_symbol]
            up, down = float(snapshot.columns["limit_up"][slot]), float(snapshot.columns["limit_down"][slot])
            order_price = max(min(order_price, up), down)
        return order_price

    def _remaining_if_fit(self, active: list[str], direction: Direction, price: float, pricetick: float) -> int | None:
        """可沿用挂单的剩余量；没有价格时只看方向与数量"""
        remaining = 0
        for vt_orderid in active:
            req = self.order_requests[vt_orderid]
            if req.direction != direction or (np.isfinite(price) and abs(req.price - price) > self.execution.reprice_ticks * pricetick):
                return None
            remaining += self._unbooked(vt_orderid)
        return remaining

    def _unbooked(self, vt_orderid: str) -> int:
        """委托还会进持仓的量"""
        order = self.orders.get(vt_orderid)
        final = order.traded if order is not None and not order.is_active() else self.order_requests[vt_orderid].volume
        return int(final) - sum(self._booked.get(vt_orderid, {}).values())

    def _send_to_target(
        self, strategy: StrategyTemplate, vt_symbol: str, direction: Direction, price: float, volume: int
    ) -> list[str]:
        """补发差额"""
        close = min(volume, self._closable(self.books[strategy.strategy_name], vt_symbol, direction))
        sent = []
        for offset, part in ((Offset.CLOSE, close), (Offset.OPEN, volume - close)):
            if part > 0:
                sent += self.send_order(strategy, vt_symbol, direction, offset, price, part)
        return sent

    def subscribe_options(self, strategy: StrategyTemplate, portfolio_name: str, option_filter: OptionFilter) -> bool:
        """登记关注范围"""
        book = self.books[strategy.strategy_name]
        if portfolio_name not in book.portfolios:
            return False
        book.scopes.append((portfolio_name, option_filter))
        self.refresh_watched(strategy)
        return True

    def subscribe_data(self, strategy: StrategyTemplate, vt_symbol: str) -> bool:
        """加入关注列表"""
        if vt_symbol not in self.manager.slot_index:
            return False
        self.books[strategy.strategy_name].watch_list.add(vt_symbol)
        self.refresh_watched(strategy)
        return True

    def watched_slots(self, strategy: StrategyTemplate) -> np.ndarray:
        """当前关注的槽位"""
        book = self.books[strategy.strategy_name]
        slot_index = self.manager.slot_index
        slots = {slot_index[vt_symbol] for vt_symbol in book.watch_list if vt_symbol in slot_index}
        for portfolio, option_filter in self._scoped_portfolios(strategy):
            slots.update(o.slot for o in option_filter.options(portfolio))
            for chain in option_filter.chains(portfolio):
                underlying = slot_index.get(f"{chain.underlying_symbol}.{chain.exchange.value}")
                if underlying is not None:
                    slots.add(underlying)
        slots.update(int(s) for s in np.flatnonzero(book.positions))
        slots.update(slot_index[self.order_requests[i].vt_symbol] for i in book.active_orderids)
        return np.array(sorted(slots), dtype=np.int64)

    def _scoped_portfolios(self, strategy: StrategyTemplate) -> list[tuple[PortfolioData, OptionFilter]]:
        """关注范围里当日存在的组合；不存在的当日提醒一次"""
        book = self.books[strategy.strategy_name]
        scoped: list[tuple[PortfolioData, OptionFilter]] = []
        for portfolio_name, option_filter in book.scopes:
            portfolio: PortfolioData | None = book.portfolios.get(portfolio_name)
            if portfolio is not None:
                scoped.append((portfolio, option_filter))
            elif ("组合不存在", strategy.strategy_name, portfolio_name) not in self._warned:
                self._warned.add(("组合不存在", strategy.strategy_name, portfolio_name))
                self.write_log(f"关注范围里的期权组合今日不存在，已跳过：{portfolio_name}", strategy)
        return scoped

    def refresh_watched(self, strategy: StrategyTemplate) -> list[str]:
        """更新关注范围"""
        book = self.books[strategy.strategy_name]
        manager: ContractManager = self.manager
        atm: np.ndarray | None = self.state.chain_arrays.get("atm_call")
        scoped: list[int] = [c.chain_id for portfolio, _ in self._scoped_portfolios(strategy) for c in portfolio.chains.values()]
        every_step: bool = any(
            f.strike_range is not None and f.strike_range.kind in (StrikeRangeKind.ATM_PERCENT, StrikeRangeKind.DELTA)
            for _, f in book.scopes
        )
        key: tuple = (
            atm[scoped].tobytes() if atm is not None else b"",
            np.flatnonzero(book.positions).tobytes(),
            frozenset(book.active_orderids),
            len(book.scopes),
            frozenset(book.watch_list),
            self.state.snapshot.seq if every_step and self.state.snapshot else None,
        )
        if book.watch_key == key:
            return []
        book.watch_key = key

        watched: list[str] = [manager.slots[s] for s in self.watched_slots(strategy)]
        new: set[str] = set(watched)
        old: set[str] = book.watched
        book.watched = new

        new_chains: set[int] = self.chains_of(new)
        old_chains: set[int] = self.chains_of(old)
        if new_chains != old_chains:
            added: str = "、".join(sorted(map(manager.chain_symbol, new_chains - old_chains))) or "无"
            removed: str = "、".join(sorted(map(manager.chain_symbol, old_chains - new_chains))) or "无"
            self.write_log(f"关注范围变化：新增链 {added}；移出链 {removed}", strategy)
        return watched

    def chains_of(self, vt_symbols: set[str]) -> set[int]:
        """合约所属的链"""
        manager: ContractManager = self.manager
        chain_ids: np.ndarray = manager.chain_of_slot[[manager.slot_index[vt_symbol] for vt_symbol in vt_symbols]]
        return set(chain_ids[chain_ids >= 0].tolist())

    def push_tick(self, tick: TickData) -> None:
        """推送 tick"""
        vt_symbol = tick.vt_symbol
        for name, book in self.books.items():
            if vt_symbol in book.watch_list:
                strategy = self.strategies[name]
                if strategy.inited:
                    self.call_strategy_func(strategy, strategy.on_tick, tick)

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """推送分钟 K 线"""
        for name, book in list(self.books.items()):
            strategy = self.strategies[name]
            if not strategy.inited:
                continue
            strategy_bars: dict[str, BarData] = {k: v for k, v in bars.items() if k in book.watched}
            if strategy_bars:
                self.call_strategy_func(strategy, strategy.on_bars, strategy_bars)

    def load_bars(self, strategy: StrategyTemplate, vt_symbol: str, days: int, interval: Interval) -> None:
        """加载历史数据"""
        for bar in self.history.load_bar(vt_symbol, self.clock.now(), days, interval):
            self.call_strategy_func(strategy, strategy.on_bars, {vt_symbol: bar})

    def get_today(self) -> date | None:
        """查询当前交易日"""
        return self.state.trading_day

    def get_pricetick(self, strategy: StrategyTemplate, vt_symbol: str) -> float | None:
        """获取合约价格跳动"""
        contract: ContractData | None = self.manager.contracts.get(vt_symbol)
        return contract.pricetick if contract else None

    def get_size(self, strategy: StrategyTemplate, vt_symbol: str) -> float | None:
        """获取合约乘数"""
        contract: ContractData | None = self.manager.contracts.get(vt_symbol)
        return contract.size if contract else None

    def settle_expiring_positions(self) -> list[tuple[Settlement, float]]:
        """到期结算"""
        manager = self.manager
        underlying = self.state.slot_arrays.get("underlying")
        results: list[tuple[Settlement, float]] = []
        for name, book in self.books.items():
            pos = book.positions
            for slot in self._expiring_slots(book):
                vt_symbol = manager.slots[slot]
                profile = manager.profile_of[vt_symbol]
                price = float(underlying[slot]) if underlying is not None else float("nan")
                volume = int(pos[slot])
                try:
                    settlement = profile.settlement.settle_at_expiry(vt_symbol, volume, price)
                except Exception:
                    self._plugin_error(profile)
                    continue
                self._move_position(book, slot, -settlement.closed_volume, price)
                for target, target_volume in settlement.new_positions.items():
                    target_slot = manager.slot_index.get(target)
                    if target_slot is None:
                        self._drop_position(name, target, target_volume)
                    else:
                        self._move_position(book, target_slot, target_volume, price)   # 行权得到的持仓按标的价记成本
                self.write_log(f"到期结算：{name} {vt_symbol} 持仓 {volume} 手，标的价 {price}，现金 {settlement.cash}")
                results.append((settlement, price))
        return results


class OptionStrategyEngine(BaseEngine, StrategyEngineBase):
    """期权策略实盘引擎"""

    engine_type: EngineType = EngineType.LIVE

    setting_filename: str = "option_strategy_setting.json"
    data_filename: str = "option_strategy_data.json"

    interval_seconds: float = INTERVAL_SECONDS
    subscribe_rate: int = 200
    tick_slice_seconds: float = 0.05
    future_tick_seconds: float = 10.0

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """构造函数"""
        super().__init__(main_engine, event_engine, APP_NAME)
        StrategyEngineBase.__init__(self)

        self.strategy_setting: dict[str, dict] = {}
        self.strategy_data: dict[str, dict] = {}
        self.classes: dict[str, type[StrategyTemplate]] = {}

        self.clock: WallClock = WallClock()
        self.channel: GatewayChannel = GatewayChannel(main_engine)
        self.source: TickSnapshotSource | None = None

        self.subscribed: set[str] = set()
        self.pending_subscriptions: deque[str] = deque()
        self.ticks: deque[TickData] = deque()
        self.tick_task_pending: bool = False
        self.bar_generator: OptionBarGenerator = OptionBarGenerator(self.on_bars)

        self.tasks: Queue = Queue()
        self.worker: Thread | None = None
        self.active: bool = False

        self.database: BaseDatabase = get_database()
        self.datafeed: BaseDatafeed = get_datafeed()
        self.history: HistoryProvider = HistoryProvider(
            self.database, self.datafeed, self.write_log, MinuteCache(default_cache_path())
        )

    def init_engine(self) -> None:
        """初始化引擎"""
        self.init_datafeed()
        self.load_profiles()
        self.load_strategy_class()
        self.load_strategy_setting()
        self.load_strategy_data()
        self.register_event()
        self.start_worker()
        self.write_log("期权策略引擎初始化成功")

    def init_datafeed(self) -> None:
        """初始化数据服务"""
        result: bool = self.datafeed.init(self.write_log)
        if result:
            self.write_log("数据服务初始化成功")

    def register_event(self) -> None:
        """注册事件引擎"""
        self.event_engine.register(EVENT_TICK, self.process_tick_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)

        log_engine: LogEngine = self.main_engine.get_engine("log")
        log_engine.register_log(EVENT_OPTION_LOG)

    def close(self) -> None:
        """关闭引擎"""
        self.stop_all_strategies()
        if self.worker:
            self.put_task(self.stop_worker)
            self.worker.join()
        for profile in self.profiles:
            profile.close()

    def start_worker(self) -> None:
        """启动工作线程"""
        self.active = True
        self.worker = Thread(target=self.run, daemon=True)
        self.worker.start()

    def stop_worker(self) -> None:
        """停止工作线程"""
        self.active = False

    def run(self) -> None:
        """工作线程主循环"""
        next_interval: float = monotonic() + self.interval_seconds
        while self.active:
            try:
                func, args = self.tasks.get(timeout=max(next_interval - monotonic(), 0))
                self.execute_task(func, args)
            except Empty:
                pass
            if monotonic() >= next_interval:
                self.execute_task(self.on_interval, ())
                next_interval = monotonic() + self.interval_seconds

    def put_task(self, func: Callable, *args: object) -> None:
        """把任务交给工作线程"""
        self.tasks.put((func, args))

    def execute_task(self, func: Callable, args: tuple) -> None:
        """执行一个任务"""
        try:
            func(*args)
        except Exception:
            self.write_log(f"引擎任务出错，触发异常：\n{traceback.format_exc()}")

    def load_strategy_class(self) -> None:
        """加载策略类"""
        path1: Path = Path(__file__).parent.joinpath("strategies")
        self.load_strategy_class_from_folder(path1, "open_optionstrategy.strategies")

        path2: Path = Path.cwd().joinpath("strategies")
        self.load_strategy_class_from_folder(path2, "strategies")

    def load_strategy_class_from_folder(self, path: Path, module_name: str = "") -> None:
        """通过指定文件夹加载策略类"""
        for suffix in ["py", "pyd", "so"]:
            pathname: str = str(path.joinpath(f"*.{suffix}"))
            for filepath in glob.glob(pathname):
                stem: str = Path(filepath).stem
                strategy_module_name: str = f"{module_name}.{stem}"
                self.load_strategy_class_from_module(strategy_module_name)

    def load_strategy_class_from_module(self, module_name: str) -> None:
        """通过策略文件加载策略类"""
        try:
            module: ModuleType = importlib.import_module(module_name)

            for name in dir(module):
                value = getattr(module, name)
                if isinstance(value, type) and issubclass(value, StrategyTemplate) and value is not StrategyTemplate:
                    if self.classes.get(value.__name__, value) is not value:
                        self.write_log(f"策略类{value.__name__}被{module_name}里的同名类替换")
                    self.classes[value.__name__] = value
        except Exception:
            msg: str = f"策略文件{module_name}加载失败，触发异常：\n{traceback.format_exc()}"
            self.write_log(msg)

    def get_all_strategy_class_names(self) -> list:
        """获取所有加载策略类名"""
        return list(self.classes.keys())

    def get_strategy_class_parameters(self, class_name: str) -> dict:
        """获取策略类参数"""
        return self.classes[class_name].get_class_parameters()

    def get_strategy_parameters(self, strategy_name: str) -> dict:
        """获取策略参数"""
        return self.strategies[strategy_name].get_parameters()

    def load_strategy_setting(self) -> None:
        """加载策略配置"""
        self.strategy_setting = self.load_json_file(self.setting_filename, "策略配置")
        for strategy_name, config in self.strategy_setting.items():
            missing: list[str] = [key for key in ("class_name", "gateway_name", "setting") if key not in config]
            if missing:
                self.write_log(f"策略配置缺字段，跳过：{strategy_name}：{'、'.join(missing)}")
                continue
            self.add_strategy(config["class_name"], strategy_name, config["gateway_name"], config["setting"])

    def update_strategy_setting(self, strategy: StrategyTemplate) -> None:
        """保存策略配置"""
        self.strategy_setting[strategy.strategy_name] = {
            "class_name": strategy.__class__.__name__,
            "gateway_name": strategy.gateway_name,
            "setting": strategy.get_parameters(),
        }
        save_json(self.setting_filename, self.strategy_setting)

    def load_strategy_data(self) -> None:
        """加载策略数据"""
        self.strategy_data = self.load_json_file(self.data_filename, "策略数据")

    def load_json_file(self, filename: str, label: str) -> dict:
        """读 JSON 配置文件"""
        try:
            return load_json(filename)
        except ValueError:
            self.write_log(f"{label}文件 {filename} 不是有效的 JSON，引擎未启动，请修复或移走该文件")
            raise

    def sync_strategy_data(self, strategy: StrategyTemplate) -> None:
        """保存策略数据"""
        strategy_name: str = strategy.strategy_name
        book: StrategyBook = self.books[strategy_name]
        if not book.restored:   # 数据恢复完成前账本是空的，写了会覆盖数据文件里的持仓与委托
            return

        data: dict = strategy.get_variables()
        data.pop("inited")
        data.pop("trading")
        data["pos_data"] = book.dropped_positions | self.get_pos_data(strategy)
        data["target_data"] = dict(book.targets)
        data["combo_data"] = {name: {"legs": legs, "target": target} for name, (legs, target) in book.combos.items()}
        data["orders"] = {vt_orderid: self._booked.get(vt_orderid, {}) for vt_orderid in sorted(book.active_orderids)}
        data["day_data"] = self._day_data(book)
        data["cost_data"] = dict(book.costs)
        try:
            json.dumps(data)
        except (TypeError, ValueError) as e:   # 写不进 JSON 的不放进各策略共用的数据字典，免得此后谁存盘都失败
            raise ValueError(f"策略数据无法写入数据文件：{e}") from e

        self.strategy_data[strategy_name] = data
        save_json(self.data_filename, self.strategy_data)

    def _day_data(self, book: StrategyBook) -> dict:
        """账本的当日部分"""
        opening: dict[str, int] | None = None
        marks: dict[str, float] = {}
        if book.positions_open is not None:
            slots = self.manager.slots
            held = np.flatnonzero(book.positions_open)
            opening = {slots[i]: int(book.positions_open[i]) for i in held}
            marks = {slots[i]: float(book.open_mark[i]) for i in held if np.isfinite(book.open_mark[i])}
        return {
            "trading_day": str(self.state.trading_day), "cash_today": book.cash_today, "positions_open": opening,
            "open_marks": marks,
        }

    def save_strategy_file(self, file_name: str, data: dict) -> None:
        """保存策略自定义数据"""
        save_json(file_name, data)

    def load_strategy_file(self, file_name: str) -> dict:
        """读取策略自定义数据"""
        return load_json(file_name)

    def add_strategy(self, class_name: str, strategy_name: str, gateway_name: str, setting: dict) -> None:
        """添加策略实例"""
        if strategy_name in self.strategies:
            self.write_log(f"创建策略失败，存在重名{strategy_name}")
            return

        strategy_class: type[StrategyTemplate] | None = self.classes.get(class_name, None)
        if not strategy_class:
            self.write_log(f"创建策略失败，找不到策略类{class_name}")
            return

        strategy: StrategyTemplate = strategy_class(self, strategy_name, gateway_name, setting)
        self.strategies[strategy_name] = strategy

        self.update_strategy_setting(strategy)
        self.put_strategy_event(strategy)

    def edit_strategy(self, strategy_name: str, setting: dict) -> None:
        """编辑策略参数"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        strategy.update_setting(setting)

        self.update_strategy_setting(strategy)
        self.put_strategy_event(strategy)

    def remove_strategy(self, strategy_name: str) -> bool:
        """移除策略实例"""
        if strategy_name in self.books:
            self.write_log(f"策略{strategy_name}移除失败，初始化过的策略请重启后移除")
            return False

        self.strategies.pop(strategy_name)
        self.strategy_setting.pop(strategy_name, None)
        save_json(self.setting_filename, self.strategy_setting)

        self.strategy_data.pop(strategy_name, None)
        save_json(self.data_filename, self.strategy_data)

        return True

    def init_strategy(self, strategy_name: str) -> None:
        """初始化策略"""
        self.put_task(self._init_strategy, strategy_name)

    def _init_strategy(self, strategy_name: str) -> None:
        """初始化策略"""
        strategy: StrategyTemplate = self.strategies[strategy_name]

        if strategy.inited:
            self.write_log(f"{strategy_name}已经完成初始化，禁止重复操作")
            return

        if strategy_name in self.strategy_errors:
            self.write_log(f"{strategy_name}出错已停止，请重启进程后再初始化")
            return

        if strategy.gateway_name not in self.main_engine.get_all_gateway_names():
            self.write_log(f"{strategy_name}初始化失败，找不到交易接口{strategy.gateway_name}")
            return

        td_api = getattr(self.main_engine.get_gateway(strategy.gateway_name), "td_api", None)
        if td_api is not None and not td_api.contract_inited:
            self.write_log(f"{strategy_name}初始化失败，交易接口{strategy.gateway_name}的合约还没查完，请等合约信息查询成功后再初始化")
            return

        if not self.ensure_day():
            return

        self.write_log(f"{strategy_name}开始执行初始化")
        self.register_strategy(strategy)

        self.call_strategy_func(strategy, strategy.on_init)
        if strategy_name in self.strategy_errors:
            return

        data: dict | None = self.strategy_data.get(strategy_name, None)
        if data:
            try:
                self.restore_data(strategy, data)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                msg: str = f"数据文件 {self.data_filename} 里的数据无法恢复，已停止：{e!r}"
                self.write_log(msg, strategy)
                self.strategy_errors[strategy_name] = msg
                self.put_strategy_event(strategy)
                return
        self.books[strategy_name].restored = True
        self.call_strategy_func(strategy, self.sync_strategy_data, strategy)   # 恢复时补记的成交在恢复完成前没有落盘
        if strategy_name in self.strategy_errors:
            return

        self.refresh_watched(strategy)
        self.history.release()

        strategy.inited = True
        self.put_strategy_event(strategy)
        self.write_log(f"{strategy_name}初始化完成")

    def restore_data(self, strategy: StrategyTemplate, data: dict) -> None:
        """恢复策略数据"""
        for name in strategy.variables:
            value: object | None = data.get(name)
            if value is not None:
                setattr(strategy, name, value)
        self.books[strategy.strategy_name].costs = {vt_symbol: float(cost) for vt_symbol, cost in data.get("cost_data", {}).items()}
        self.load_pos_data(strategy, data.get("pos_data", {}))
        for vt_symbol, target in data.get("target_data", {}).items():
            self.set_target(strategy, vt_symbol, target)
        for name, combo in data.get("combo_data", {}).items():
            self.set_combo_target(strategy, name, combo["legs"], combo["target"])
        day: dict | None = data.get("day_data")
        if day and day["trading_day"] == str(self.state.trading_day):
            self._restore_day(self.books[strategy.strategy_name], day)
        for vt_orderid, booked in data.get("orders", {}).items():
            order: OrderData | None = self.main_engine.get_order(vt_orderid)
            if order:
                self.adopt_order(strategy, order, booked)
        for trade in self.main_engine.get_all_trades():
            if self.order_owner.get(trade.vt_orderid) == strategy.strategy_name:
                self.process_trade(trade)

    def _restore_day(self, book: StrategyBook, day: dict) -> None:
        """接回当日部分"""
        book.cash_today = float(day["cash_today"])
        if day["positions_open"] is None:
            return
        slot_index = self.manager.slot_index
        book.positions_open = np.zeros_like(book.positions)
        for vt_symbol, volume in day["positions_open"].items():
            if vt_symbol in slot_index:
                book.positions_open[slot_index[vt_symbol]] = int(volume)
        for vt_symbol, mark in day["open_marks"].items():
            if vt_symbol in slot_index:
                book.open_mark[slot_index[vt_symbol]] = float(mark)

    def start_strategy(self, strategy_name: str) -> None:
        """启动策略"""
        self.put_task(self._start_strategy, strategy_name)

    def _start_strategy(self, strategy_name: str) -> None:
        """启动策略"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        if not strategy.inited:
            self.write_log(f"策略{strategy_name}启动失败，请先初始化")
            return

        if strategy.trading:
            self.write_log(f"{strategy_name}已经启动，请勿重复操作")
            return

        self.cancel_all(strategy)

        self.call_strategy_func(strategy, strategy.on_start)
        if not strategy.inited:
            return

        strategy.trading = True
        self.put_strategy_event(strategy)

    def stop_strategy(self, strategy_name: str) -> None:
        """停止策略"""
        self.put_task(self._stop_strategy, strategy_name)

    def _stop_strategy(self, strategy_name: str) -> None:
        """停止策略"""
        strategy: StrategyTemplate = self.strategies[strategy_name]
        if not strategy.trading:
            return

        self.call_strategy_func(strategy, strategy.on_stop)

        strategy.trading = False

        self.cancel_all(strategy)

        self.sync_strategy_data(strategy)

        self.put_strategy_event(strategy)

    def init_all_strategies(self) -> None:
        """初始化所有策略"""
        for strategy_name in self.strategies:
            self.init_strategy(strategy_name)

    def start_all_strategies(self) -> None:
        """启动所有策略"""
        for strategy_name in self.strategies:
            self.start_strategy(strategy_name)

    def stop_all_strategies(self) -> None:
        """停止所有策略"""
        for strategy_name in self.strategies:
            self.stop_strategy(strategy_name)

    def ensure_day(self) -> bool:
        """建当日状态"""
        if self.manager is not None:
            return True

        contracts: list[ContractData] = self.main_engine.get_all_contracts()
        routed: dict[str, MarketProfile] = route_profiles(contracts, self.profiles)
        if not routed:
            self.write_log("没有规则实现认领的合约：请确认交易接口已推送合约信息，并已配置规则实现")
            return False

        unclaimed: set[str] = {
            c.option_portfolio for c in contracts if c.product == Product.OPTION and c.vt_symbol not in routed
        }
        if unclaimed:
            self.write_log("以下期权组合没有规则实现认领，不可用：{}".format("、".join(sorted(unclaimed))))

        self.build_day(contracts, routed)
        for profile in self.profiles:
            profile.on_start(self.main_engine)
        return True

    @staticmethod
    def day_calendar(profile_of: dict[str, MarketProfile], slots: list[str]) -> Calendar:
        """定交易日的日历"""
        return profile_of[slots[0]].calendar

    def build_day(self, contracts: list[ContractData], routed: dict[str, MarketProfile]) -> None:
        """建当日槽位表与状态"""
        now: datetime = self.clock.now()
        expired: set[str] = set()
        for profile in self.profiles:
            claimed: list[ContractData] = [c for c in contracts if routed.get(c.vt_symbol) is profile]
            expiry: np.ndarray = profile.calendar.expiry_timestamps(claimed)
            expired.update(c.vt_symbol for c, ts in zip(claimed, expiry, strict=True) if ts < now.timestamp())
        mine: list[ContractData] = [c for c in contracts if c.vt_symbol in routed and c.vt_symbol not in expired]
        slots: list[str] = [c.vt_symbol for c in mine]
        trading_day: date = self.day_calendar(routed, slots).trading_day_of(now)

        self.source = TickSnapshotSource(self.clock, slots)
        self.start_day(mine, trading_day)
        self.write_log(f"交易日{trading_day}，规则实现认领合约{len(slots)}个")

    def roll_day(self) -> None:
        """换日"""
        self.bar_generator.flush()
        contracts: list[ContractData] = self.main_engine.get_all_contracts()
        routed: dict[str, MarketProfile] = route_profiles(contracts, self.profiles)
        if not routed:
            self.write_log("换日失败：没有规则实现认领的合约，沿用旧交易日")
            return
        stale: int = 0
        for book in self.books.values():
            stale += len(book.active_orderids)
            book.active_orderids.clear()
        self._cancelling.clear()
        if stale:
            self.write_log(f"换日：上一交易日未了结的委托 {stale} 笔已失效")
        self.orders.clear()
        self.order_requests.clear()
        self.order_owner.clear()
        self._booked.clear()
        self.history.today_bars.clear()
        self.build_day(contracts, routed)
        for strategy in list(self.strategies.values()):
            if strategy.inited:
                self.refresh_watched(strategy)

    def process_tick_event(self, event: Event) -> None:
        """行情推送"""
        self.ticks.append(event.data)
        if not self.tick_task_pending:
            self.tick_task_pending = True
            self.put_task(self.process_ticks)

    def process_ticks(self) -> None:
        """处理缓冲里的 tick"""
        self.tick_task_pending = False
        deadline: float = monotonic() + self.tick_slice_seconds
        latest: float = self.clock.now().timestamp() + self.future_tick_seconds
        while self.ticks:
            tick: TickData = self.ticks.popleft()
            if tick.datetime.timestamp() > latest:
                if ("tick 时间超前", tick.gateway_name, "") not in self._warned:
                    self._warned.add(("tick 时间超前", tick.gateway_name, ""))
                    self.write_log(f"丢弃时间超前的 tick：首笔 {tick.vt_symbol} {tick.datetime}，请核对本机时钟，当日只提醒一次")
                continue
            self.process_tick(tick)
            if monotonic() >= deadline:
                break
        if self.ticks and not self.tick_task_pending:
            self.tick_task_pending = True
            self.put_task(self.process_ticks)

    def process_tick(self, tick: TickData) -> None:
        """处理一笔 tick"""
        if not self.source:
            return
        self.source.update_tick(tick)
        self.bar_generator.update_tick(tick)
        self.push_tick(tick)

    def process_order_event(self, event: Event) -> None:
        """委托推送"""
        self.put_task(self.process_order, event.data)

    def process_trade_event(self, event: Event) -> None:
        """成交推送"""
        self.put_task(self.process_trade, event.data)

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """分钟 K 线合成完毕"""
        for vt_symbol, bar in bars.items():
            self.history.today_bars[vt_symbol].append(bar)
        super().on_bars(bars)

    def on_interval(self) -> None:
        """每个节拍"""
        self.send_subscriptions()
        manager: ContractManager | None = self.manager
        if manager is None:
            return

        if self.day_calendar(manager.profile_of, manager.slots).trading_day_of(self.clock.now()) != self.state.trading_day:
            self.roll_day()
        self.bar_generator.close_due(self.clock.now())

        self.step()
        for strategy in list(self.strategies.values()):
            if strategy.inited:
                self.refresh_watched(strategy)

    def refresh_watched(self, strategy: StrategyTemplate) -> list[str]:
        """更新关注范围并排订阅"""
        watched: list[str] = super().refresh_watched(strategy)
        for vt_symbol in watched:
            if vt_symbol not in self.subscribed:
                self.subscribed.add(vt_symbol)
                self.pending_subscriptions.append(vt_symbol)
        return watched

    def send_subscriptions(self) -> None:
        """匀速发送订阅"""
        budget: int = int(self.subscribe_rate * self.interval_seconds)
        while budget and self.pending_subscriptions:
            vt_symbol: str = self.pending_subscriptions.popleft()
            contract: ContractData = self.main_engine.get_contract(vt_symbol)
            req: SubscribeRequest = SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange)
            self.main_engine.subscribe(req, contract.gateway_name)
            budget -= 1

    def write_log(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """输出日志"""
        if strategy:
            msg = f"{strategy.strategy_name}: {msg}"

        log: LogData = LogData(msg=msg, gateway_name=APP_NAME)
        event: Event = Event(type=EVENT_OPTION_LOG, data=log)
        self.event_engine.put(event)

    def put_strategy_event(self, strategy: StrategyTemplate) -> None:
        """推送事件更新策略界面"""
        data: dict = strategy.get_data()
        event: Event = Event(EVENT_OPTION_STRATEGY, data)
        self.event_engine.put(event)

    def send_notification(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """通过已配置渠道推送通知"""
        if strategy:
            subject: str = f"{strategy.strategy_name}"
        else:
            subject = "期权策略引擎"

        self.main_engine.send_notification(msg, subject)

"""期权数据模型与规则协议"""
from bisect import bisect_left
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, Protocol

import numpy as np
from vnpy.trader.constant import Exchange, OptionType
from vnpy.trader.object import CancelRequest, ContractData, OrderRequest, TradeData

from .base import GREEK_NAMES, DayType, ExerciseStyle, Quality, StrikeRangeKind

ENTRY_POINT_GROUP = "open_optionstrategy.profiles"


@dataclass(frozen=True)
class Snapshot:
    """全市场截面"""

    columns: dict[str, np.ndarray]
    seq: int
    datetime: datetime
    slots: list[str]

    @property
    def slot_count(self) -> int:
        return len(self.slots)


@dataclass(frozen=True)
class OptionAttributes:
    """期权合约属性"""

    exercise_style: ExerciseStyle
    last_exercise_date: date | None
    max_order_volume: int
    min_open_volume: int


@dataclass(frozen=True)
class Settlement:
    """到期结算结果"""

    vt_symbol: str
    closed_volume: int
    new_positions: dict[str, int]
    cash: float


@dataclass(frozen=True)
class ExecutionSettings:
    """执行参数"""

    reprice_ticks: int = 2
    min_reprice_seconds: float = 3.0
    cancel_retry_seconds: float = 5.0


class Clock(Protocol):
    """时钟"""

    def now(self) -> datetime: ...


class SnapshotSource(Protocol):
    """快照来源"""

    def latest(self) -> Snapshot | None: ...


class ExecutionChannel(Protocol):
    """执行通道"""

    def convert_order_request(self, req: OrderRequest, gateway_name: str) -> list[OrderRequest]: ...

    def send_order(self, req: OrderRequest, gateway_name: str) -> str: ...

    def cancel_order(self, req: CancelRequest, gateway_name: str) -> None: ...

    def available(self, gateway_name: str) -> float | None:
        """账户可用资金"""
        ...


class PricingModel(Protocol):
    """定价模型"""

    def price(self, f: np.ndarray, k: np.ndarray, t: np.ndarray, r: float, v: np.ndarray, cp: np.ndarray) -> np.ndarray: ...

    def greeks(
        self, f: np.ndarray, k: np.ndarray, t: np.ndarray, r: float, v: np.ndarray, cp: np.ndarray
    ) -> dict[str, np.ndarray]: ...

    def implied_vol(
        self, p: np.ndarray, f: np.ndarray, k: np.ndarray, t: np.ndarray, r: float, cp: np.ndarray,
        v0: np.ndarray | None = None,
    ) -> np.ndarray: ...


class Calendar(Protocol):
    """交易日历"""

    def trading_day_of(self, dt: datetime) -> date: ...

    def trading_days_between(self, start: date, end: date) -> int: ...

    def expiry_timestamps(self, contracts: list[ContractData]) -> np.ndarray: ...


class ContractInfo(Protocol):
    """合约信息"""

    def chain_key(self, contract: ContractData) -> tuple[str, str, date] | None:
        """期权所属的组合与链"""
        ...

    def series_of(self, contract: ContractData) -> str: ...

    def contract_attributes(self, contract: ContractData) -> OptionAttributes: ...

    def pricing_model(self, contract: ContractData) -> PricingModel:
        """合约的定价模型"""
        ...


class UnderlyingPricer(Protocol):
    """标的价"""

    def build_plan(self, contracts: list[ContractData], slots: dict[str, int]) -> Any: ...

    def underlying_prices(self, plan: Any, snapshot: Snapshot) -> np.ndarray: ...


class MarginModel(Protocol):
    """保证金模型"""

    def margins(
        self, price: np.ndarray, underlying: np.ndarray, strike: np.ndarray, cp: np.ndarray, size: np.ndarray,
        is_option: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]: ...


class CostModel(Protocol):
    """手续费模型"""

    def commission(self, trade: TradeData) -> float: ...


class SettlementModel(Protocol):
    """到期结算模型"""

    def settle_at_expiry(self, vt_symbol: str, volume: int, settle_price: float) -> Settlement: ...


class MarketProfile(Protocol):
    """规则实现"""

    name: str
    version: str
    calendar: Calendar
    contract_info: ContractInfo
    underlying: UnderlyingPricer
    margin_model: MarginModel
    cost_model: CostModel
    settlement: SettlementModel

    def match(self, contract: ContractData) -> bool: ...

    def on_start(self, main_engine: Any | None) -> None: ...

    def close(self) -> None: ...


def load_profiles(names: list[str]) -> list[MarketProfile]:
    """加载规则实现"""
    targets = list(names) + [ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)]
    profiles: list[MarketProfile] = []
    for target in dict.fromkeys(targets):
        module_name, attr = target.split(":")
        obj = getattr(import_module(module_name), attr)
        profiles.append(obj() if isinstance(obj, type) else obj)
    return profiles


def route_profiles(contracts: list[ContractData], profiles: list[MarketProfile]) -> dict[str, MarketProfile]:
    """为合约找规则实现"""
    routed: dict[str, MarketProfile] = {}
    for contract in contracts:
        for profile in profiles:
            if profile.match(contract):
                routed[contract.vt_symbol] = profile
                break
    return routed


def is_trading_day(calendar: Calendar, day: date) -> bool:
    """是否交易日"""
    return calendar.trading_day_of(datetime.combine(day, time(12))) == day


def previous_trading_day(calendar: Calendar, day: date) -> date:
    """上一个交易日"""
    for n in range(1, 31):
        previous = day - timedelta(days=n)
        if is_trading_day(calendar, previous):
            return previous
    raise ValueError(f"{day} 之前 30 天内日历没有交易日")


def trading_day_start(calendar: Calendar, day: date) -> datetime:
    """交易日开始时刻"""
    low: datetime = datetime.combine(previous_trading_day(calendar, day), time(12))
    high: datetime = datetime.combine(day, time(12))
    while high - low > timedelta(minutes=1):
        middle: datetime = low + timedelta(minutes=(high - low) // timedelta(minutes=1) // 2)
        if calendar.trading_day_of(middle) == day:
            high = middle
        else:
            low = middle
    return high


def pick_nearest(k_lo: np.ndarray, k_hi: np.ndarray, price: np.ndarray, prefer_higher: bool) -> np.ndarray:
    """取离价格近的行权价"""
    d_lo = np.abs(price - k_lo)
    d_hi = np.abs(k_hi - price)
    return np.where(d_lo < d_hi, k_lo, np.where(d_hi < d_lo, k_hi, k_hi if prefer_higher else k_lo))


def nearest_strike(strikes: list[float], price: float, prefer_higher: bool) -> float | None:
    """离价格最近的行权价"""
    if not strikes or not np.isfinite(price):
        return None
    j = bisect_left(strikes, price)
    if j == 0:
        return strikes[0]
    if j == len(strikes):
        return strikes[-1]
    return float(pick_nearest(np.float64(strikes[j - 1]), np.float64(strikes[j]), np.float64(price), prefer_higher))


def _atm_window(strikes: list[float], price: float, below: int, above: int) -> list[float]:
    """平值上下若干档"""
    if not np.isfinite(price):
        return []
    i = strikes.index(nearest_strike(strikes, price, prefer_higher=True))
    return strikes[max(0, i - below): i + above + 1]


class MarketState:
    """当前节拍的市场状态"""

    def __init__(self) -> None:
        self.snapshot: Snapshot | None = None
        self.trading_day: date | None = None
        self.slot_arrays: dict[str, np.ndarray] = {}
        self.chain_arrays: dict[str, np.ndarray] = {}

    def column(self, name: str, slot: int) -> float:
        if self.snapshot is None or name not in self.snapshot.columns:
            return float("nan")
        return float(self.snapshot.columns[name][slot])

    def slot_value(self, name: str, slot: int) -> float:
        array = self.slot_arrays.get(name)
        return float("nan") if array is None else float(array[slot])

    def chain_value(self, name: str, chain_id: int) -> float:
        array = self.chain_arrays.get(name)
        return float("nan") if array is None else float(array[chain_id])


def _column_field(name: str) -> property:
    """快照列视图字段"""
    return property(lambda self: self.portfolio.state.column(name, self.slot))


def _slot_field(name: str) -> property:
    """槽位数组视图字段"""
    return property(lambda self: self.portfolio.state.slot_value(name, self.slot))


def _attribute_field(name: str) -> property:
    """期权属性视图字段"""
    return property(lambda self: getattr(self.attributes, name))


class OptionData:
    """单个期权视图"""

    __slots__ = ("vt_symbol", "contract", "slot", "chain", "portfolio", "strike", "cp", "attributes")

    price = _column_field("last")
    bid = _column_field("bid1")
    ask = _column_field("ask1")
    volume = _column_field("volume")
    open_interest = _column_field("open_interest")
    iv = _slot_field("iv")
    bid_iv = _slot_field("bid_iv")
    ask_iv = _slot_field("ask_iv")
    delta = _slot_field("delta")
    delta_unit = _slot_field("delta_unit")
    gamma = _slot_field("gamma")
    vega = _slot_field("vega")
    theta = _slot_field("theta")
    exercise_style = _attribute_field("exercise_style")
    last_exercise_date = _attribute_field("last_exercise_date")
    max_order_volume = _attribute_field("max_order_volume")
    min_open_volume = _attribute_field("min_open_volume")

    def __init__(
        self, contract: ContractData, slot: int, chain: "ChainData", portfolio: "PortfolioData", attributes: OptionAttributes
    ) -> None:
        self.vt_symbol = contract.vt_symbol
        self.contract = contract
        self.slot = slot
        self.chain = chain
        self.portfolio = portfolio
        self.strike = float(contract.option_strike)
        self.cp = 1 if contract.option_type == OptionType.CALL else -1
        self.attributes = attributes

    @property
    def quality(self) -> Quality:
        """质量位"""
        value = self.portfolio.state.slot_value("quality", self.slot)
        return Quality(int(value)) if np.isfinite(value) else Quality.NONE

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def pos(self) -> int:
        return int(self.portfolio.pos[self.slot])

    def days_to_expiry(self, day_type: DayType = DayType.CALENDAR) -> int:
        return self.chain.days_to_expiry(day_type)


class ChainData:
    """期权链"""

    def __init__(
        self, chain_id: int, symbol: str, exchange: Exchange, expiry: date, series: str, underlying_symbol: str,
        portfolio: "PortfolioData",
    ) -> None:
        self.chain_id = chain_id
        self.symbol = symbol
        self.exchange = exchange
        self.expiry = expiry
        self.series = series
        self.underlying_symbol = underlying_symbol
        self.portfolio = portfolio
        self.calls: dict[float, OptionData] = {}
        self.puts: dict[float, OptionData] = {}
        self._strikes: list[float] | None = None

    @property
    def strikes(self) -> list[float]:
        if self._strikes is None:
            self._strikes = sorted(set(self.calls) | set(self.puts))
        return self._strikes

    @property
    def underlying_price(self) -> float:
        return self.portfolio.state.chain_value("underlying", self.chain_id)

    @property
    def atm_strike(self) -> float:
        return self.portfolio.state.chain_value("atm_call", self.chain_id)

    def add_contract(self, contract: ContractData, slot: int, attributes: OptionAttributes) -> OptionData:
        option = OptionData(contract, slot, self, self.portfolio, attributes)
        (self.calls if option.cp > 0 else self.puts)[option.strike] = option
        self.portfolio.options[option.vt_symbol] = option
        self._strikes = None
        return option

    def days_to_expiry(self, day_type: DayType = DayType.CALENDAR) -> int:
        today = self.portfolio.state.trading_day
        if day_type == DayType.TRADING:
            return self.portfolio.profile.calendar.trading_days_between(today, self.expiry)
        return (self.expiry - today).days

    def get_all_level_options(
        self, cp: int, underlying_price: float | None = None
    ) -> tuple[list[OptionData], list[OptionData], list[OptionData]]:
        """实值、平值、虚值期权"""
        book = self.calls if cp > 0 else self.puts
        strikes = sorted(book)
        price = self.underlying_price if underlying_price is None else underlying_price
        atm = nearest_strike(strikes, price, prefer_higher=cp > 0)
        if atm is None:
            return [], [], []
        i = strikes.index(atm)
        below = [book[k] for k in reversed(strikes[:i])]
        above = [book[k] for k in strikes[i + 1:]]
        return (below, [book[atm]], above) if cp > 0 else (above, [book[atm]], below)

    def get_option_by_level(self, cp: int, level: int) -> OptionData | None:
        """按档位取期权"""
        itm, atm, otm = self.get_all_level_options(cp)
        if not atm:
            return None
        if level == 0:
            return atm[0]
        side, index = (otm, level - 1) if level > 0 else (itm, -level - 1)
        return side[index] if index < len(side) else None

    def first_strike_above(self, price: float) -> float | None:
        return next((k for k in self.strikes if k > price), None)

    def first_strike_below(self, price: float) -> float | None:
        return next((k for k in reversed(self.strikes) if k < price), None)

    def select_by_delta(self, cp: int, target_delta: float) -> OptionData | None:
        """按 delta 选期权"""
        book = self.calls if cp > 0 else self.puts
        scored = [
            (abs(abs(option.delta_unit) - abs(target_delta)), option.strike, option)
            for option in book.values()
            if np.isfinite(option.delta)
        ]
        if not scored:
            return None
        pick = min(scored, key=lambda s: (s[0], s[1]))[2]
        strikes = sorted(book)
        i = strikes.index(pick.strike)
        return pick if all(np.isfinite(book[k].delta) for k in strikes[max(i - 1, 0): i + 2]) else None


class PortfolioData:
    """期权组合视图"""

    def __init__(
        self, symbol: str, exchange: Exchange, profile: MarketProfile, state: MarketState, pos: np.ndarray
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.profile = profile
        self._state = state
        self._pos = pos
        self.expired = False
        self.chains: dict[str, ChainData] = {}
        self.options: dict[str, OptionData] = {}

    @property
    def state(self) -> MarketState:
        self._check_expired()
        return self._state

    @property
    def pos(self) -> np.ndarray:
        self._check_expired()
        return self._pos

    def _check_expired(self) -> None:
        if self.expired:
            raise RuntimeError(f"组合视图 {self.symbol} 已随换交易日失效，请重新 get_portfolio 取当日视图")

    def add_chain(self, chain: ChainData) -> None:
        self.chains[chain.symbol] = chain

    def get_chain_by_level(self, level: int) -> list[ChainData]:
        """按到期日档位取链"""
        expiries = sorted({chain.expiry for chain in self.chains.values()})
        if not 0 <= level < len(expiries):
            return []
        return sorted((c for c in self.chains.values() if c.expiry == expiries[level]), key=lambda c: c.symbol)

    def greeks(self) -> dict[str, float]:
        """持仓希腊值合计"""
        slots = np.array([o.slot for o in self.options.values()], dtype=np.int64)
        held = slots[self.pos[slots] != 0]
        return {name: float(np.dot(self.pos[held], array[held])) if (array := self.state.slot_arrays.get(name)) is not None
                else float("nan") for name in GREEK_NAMES}


def _options_at(chain: ChainData, strikes: list[float], cp: int) -> list[OptionData]:
    """链上这些行权价的期权"""
    books = [book for side, book in ((1, chain.calls), (-1, chain.puts)) if cp in (0, side)]
    return [book[k] for k in strikes for book in books if k in book]


@dataclass(frozen=True)
class StrikeRange:
    """行权价范围"""

    kind: StrikeRangeKind
    strikes: tuple[float, ...] = ()
    below: int = 0
    above: int = 0
    lower: float = 0.0
    upper: float = 0.0
    target: float = 0.0
    tolerance: float = 0.0

    @classmethod
    def fixed(cls, strikes: list[float]) -> "StrikeRange":
        return cls(StrikeRangeKind.FIXED, strikes=tuple(float(k) for k in strikes))

    @classmethod
    def atm_relative(cls, below: int, above: int) -> "StrikeRange":
        """平值上下若干档"""
        return cls(StrikeRangeKind.ATM_RELATIVE, below=below, above=above)

    @classmethod
    def atm_percent(cls, lower: float, upper: float) -> "StrikeRange":
        """标的价上下百分比"""
        return cls(StrikeRangeKind.ATM_PERCENT, lower=lower, upper=upper)

    @classmethod
    def delta(cls, target: float, tolerance: float) -> "StrikeRange":
        """单位 delta 区间"""
        return cls(StrikeRangeKind.DELTA, target=target, tolerance=tolerance)

    def select(self, chain: ChainData) -> list[float]:
        """范围内的行权价"""
        return sorted({o.strike for o in self.options(chain)})

    def options(self, chain: ChainData, cp: int = 0) -> list[OptionData]:
        """范围内的期权"""
        strikes, price = chain.strikes, chain.underlying_price
        match self.kind:
            case StrikeRangeKind.FIXED:
                wanted = set(self.strikes)
                strikes = [k for k in strikes if k in wanted]
            case StrikeRangeKind.ATM_RELATIVE:
                strikes = _atm_window(strikes, price, self.below, self.above)
            case StrikeRangeKind.ATM_PERCENT:
                strikes = [k for k in strikes if price * (1 - self.lower) <= k <= price * (1 + self.upper)]
            case StrikeRangeKind.DELTA:
                if any(np.isfinite(o.delta) for o in [*chain.calls.values(), *chain.puts.values()]):
                    low, high = self.target - self.tolerance, self.target + self.tolerance
                    return [o for o in _options_at(chain, strikes, cp) if low <= abs(o.delta_unit) <= high]
                strikes = _atm_window(strikes, price, 5, 5)
        return _options_at(chain, strikes, cp)


@dataclass(frozen=True)
class OptionFilter:
    """合约选择条件"""

    min_days: int | None = None
    max_days: int | None = None
    day_type: DayType = DayType.CALENDAR
    series_names: frozenset[str] | None = None
    strike_range: StrikeRange | None = None
    cp: int = 0

    def days(self, min_days: int | None = None, max_days: int | None = None, day_type: DayType = DayType.CALENDAR) -> "OptionFilter":
        """剩余天数区间"""
        return replace(self, min_days=min_days, max_days=max_days, day_type=day_type)

    def series(self, names: set[str]) -> "OptionFilter":
        return replace(self, series_names=frozenset(names))

    def strikes(self, strike_range: StrikeRange) -> "OptionFilter":
        return replace(self, strike_range=strike_range)

    def calls_only(self) -> "OptionFilter":
        return replace(self, cp=1)

    def puts_only(self) -> "OptionFilter":
        return replace(self, cp=-1)

    def chains(self, portfolio: PortfolioData) -> list[ChainData]:
        """满足条件的链"""
        result = []
        for chain in sorted(portfolio.chains.values(), key=lambda c: (c.expiry, c.symbol)):
            days = chain.days_to_expiry(self.day_type)
            if (
                (self.min_days is None or days > self.min_days)
                and (self.max_days is None or days < self.max_days)
                and (self.series_names is None or chain.series in self.series_names)
            ):
                result.append(chain)
        return result

    def options(self, portfolio: PortfolioData) -> list[OptionData]:
        """满足条件的期权"""
        return [
            o for chain in self.chains(portfolio)
            for o in (self.strike_range.options(chain, self.cp) if self.strike_range else _options_at(chain, chain.strikes, self.cp))
        ]

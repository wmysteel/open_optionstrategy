"""期权策略回测引擎"""
import hashlib
import heapq
import json
import math
import traceback
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import TypeVar

import numpy as np
import plotly.graph_objects as go
from pandas import DataFrame
from plotly.subplots import make_subplots
from vnpy.trader.constant import Direction, Interval, Offset, Status
from vnpy.trader.database import DB_TZ, get_database
from vnpy.trader.datafeed import get_datafeed
from vnpy.trader.object import BarData, CancelRequest, ContractData, OrderData, OrderRequest, TickData, TradeData
from vnpy.trader.optimize import (
    OptimizationSetting,
    check_optimization_setting,
    run_bf_optimization,
    run_ga_optimization,
)
from vnpy.trader.utility import extract_vt_symbol

from .base import GREEK_COLUMNS, GREEKS_CONVENTION, INTERVAL_SECONDS, BacktestingMode, CacheFlag, EngineType
from .engine import StrategyEngineBase
from .manager import ContractManager
from .object import (
    Calendar,
    Clock,
    MarketProfile,
    is_trading_day,
    previous_trading_day,
    route_profiles,
    trading_day_start,
)
from .template import StrategyTemplate
from .utility import (
    HistoryProvider,
    MinuteCache,
    MinuteDay,
    OptionBarGenerator,
    TickCache,
    TickDay,
    TickSnapshotSource,
    default_cache_path,
    naive_time,
    same_contracts,
)

T = TypeVar("T")


STATISTICS: tuple[tuple[str, str, str], ...] = (
    ("start_date", "首个交易日", "{}"),
    ("end_date", "最后交易日", "{}"),
    ("total_days", "总交易日", "{}"),
    ("profit_days", "盈利交易日", "{}"),
    ("loss_days", "亏损交易日", "{}"),
    ("capital", "起始资金", "{:,.2f}"),
    ("end_balance", "结束资金", "{:,.2f}"),
    ("total_return", "总收益率", "{:,.2f}%"),
    ("annual_return", "年化收益", "{:,.2f}%"),
    ("max_drawdown", "最大回撤", "{:,.2f}"),
    ("max_ddpercent", "百分比最大回撤", "{:,.2f}%"),
    ("max_drawdown_duration", "最长回撤天数", "{}"),
    ("total_net_pnl", "总盈亏", "{:,.2f}"),
    ("total_commission", "总手续费", "{:,.2f}"),
    ("total_slippage", "总滑点", "{:,.2f}"),
    ("total_turnover", "总成交金额", "{:,.2f}"),
    ("total_trade_count", "总成交笔数", "{}"),
    ("daily_net_pnl", "日均盈亏", "{:,.2f}"),
    ("daily_commission", "日均手续费", "{:,.2f}"),
    ("daily_slippage", "日均滑点", "{:,.2f}"),
    ("daily_turnover", "日均成交金额", "{:,.2f}"),
    ("daily_trade_count", "日均成交笔数", "{}"),
    ("daily_return", "日均收益率", "{:,.2f}%"),
    ("return_std", "收益标准差", "{:,.2f}%"),
    ("sharpe_ratio", "Sharpe Ratio", "{:,.2f}"),
    ("return_drawdown_ratio", "收益回撤比", "{:,.2f}"),
    ("calmar_ratio", "卡玛比率", "{:,.2f}"),
    ("sortino_ratio", "索提诺比率", "{:,.2f}"),
    ("var_95", "日收益95%VaR", "{:,.2f}%"),
    ("cvar_95", "日收益95%CVaR", "{:,.2f}%"),
    ("omega_ratio", "欧米茄比率", "{:,.2f}"),
    ("round_count", "平仓回合数", "{}"),
    ("win_rate", "胜率", "{:,.2f}%"),
    ("profit_loss_ratio", "盈亏比", "{:,.2f}"),
    ("estimated_fill_ratio", "估算盘口成交占比", "{:,.2f}%"),
)


def _ratio(gain: float, risk: float) -> float:
    """收益与风险之比；没有风险时有收益为无穷大，否则为 0"""
    if risk:
        return gain / risk
    return np.inf if gain > 0 else 0.0


def get_setting(value: T | dict[str, T], contract: ContractData, default: T) -> T:
    """按合约取回测参数"""
    if not isinstance(value, dict):
        return value
    if contract.vt_symbol in value:
        return value[contract.vt_symbol]
    return value.get(contract.option_portfolio, default)


class BacktestingChannel:
    """回测撮合模拟器"""

    def __init__(
        self, clock: Clock, on_order: Callable[[OrderData], None], on_trade: Callable[[TradeData], None],
        fill_model: str | dict[str, str] = "natural", fill_ratio: float | dict[str, float] = 0.5,
        available: Callable[[], float | None] = lambda: None, fill_volume_ratio: float | dict[str, float] = 0,
    ) -> None:
        self.clock: Clock = clock
        self.on_order: Callable[[OrderData], None] = on_order
        self.on_trade: Callable[[TradeData], None] = on_trade
        self.fill_model: str | dict[str, str] = fill_model
        self.fill_ratio: float | dict[str, float] = fill_ratio
        self.fill_volume_ratio: float | dict[str, float] = fill_volume_ratio
        self._available: Callable[[], float | None] = available

        self.limit_orders: dict[str, OrderData] = {}
        self.active_limit_orders: dict[str, OrderData] = {}
        self.cancelled_orders: list[OrderData] = []

        self.trades: dict[str, TradeData] = {}
        self.estimated_trades: set[str] = set()
        self.fill_allowance: dict[str, float] = {}

    def convert_order_request(self, req: OrderRequest, gateway_name: str) -> list[OrderRequest]:
        """回测不做开平转换"""
        return [req]

    def available(self, gateway_name: str) -> float | None:
        """回测账户可用资金"""
        return self._available()

    def send_order(self, req: OrderRequest, gateway_name: str) -> str:
        """登记委托"""
        order: OrderData = req.create_order_data(str(len(self.limit_orders) + 1), gateway_name)
        order.datetime = self.clock.now()

        self.active_limit_orders[order.vt_orderid] = order
        self.limit_orders[order.vt_orderid] = order
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest, gateway_name: str) -> None:
        """撤销活动委托"""
        order: OrderData | None = self.active_limit_orders.pop(f"{gateway_name}.{req.orderid}", None)
        if order:
            order.status = Status.CANCELLED
            self.cancelled_orders.append(order)

    def push_cancelled(self) -> None:
        """推送已撤委托的回报"""
        cancelled, self.cancelled_orders = self.cancelled_orders, []
        for order in cancelled:
            self.on_order(order)

    def end_day(self) -> None:
        """日终撤销未成交委托"""
        for order in self.active_limit_orders.values():
            order.status = Status.CANCELLED
            self.cancelled_orders.append(order)
        self.active_limit_orders.clear()
        self.push_cancelled()

    def cross_limit_order(
        self, columns: dict[str, np.ndarray], manager: ContractManager, traded: np.ndarray | float, vt_symbol: str | None = None,
    ) -> None:
        """撮合活动委托"""
        self.push_cancelled()
        self._drop_idle_allowance()
        if not self.active_limit_orders:
            return

        bid: np.ndarray = columns["bid1"]
        ask: np.ndarray = columns["ask1"]
        estimated: np.ndarray | None = columns.get("est_quote")
        credited: set[str] = set()

        for order in list(self.active_limit_orders.values()):
            if vt_symbol and order.vt_symbol != vt_symbol:
                continue
            if order.status == Status.SUBMITTING:
                order.status = Status.NOTTRADED
                self.on_order(order)

            slot: int = manager.slot_index[order.vt_symbol]
            contract: ContractData = manager.contracts[order.vt_symbol]
            tolerance: float = contract.pricetick * 1e-6   # 缓存读回、估算盘口的价格带浮点尾差，差远小于一个价位的视为相等
            if order.direction == Direction.LONG:
                crossed: bool = bool(np.isfinite(ask[slot]) and order.price >= ask[slot] - tolerance)
            else:
                crossed = bool(np.isfinite(bid[slot]) and order.price <= bid[slot] + tolerance)
            if not crossed:
                continue

            volume: float = order.volume - order.traded
            ratio: float = get_setting(self.fill_volume_ratio, contract, 0)
            if ratio > 0:
                if order.vt_symbol not in credited:   # 每段按本段成交量 × 比例累计一次，同一段内的委托共用
                    credited.add(order.vt_symbol)
                    segment: float = float(traded if np.isscalar(traded) else traded[slot])
                    self.fill_allowance[order.vt_symbol] = self.fill_allowance.get(order.vt_symbol, 0.0) + max(segment, 0) * ratio
                volume = min(volume, int(self.fill_allowance[order.vt_symbol] + 1e-9))   # 容差吸收累加的浮点尾差
                self.fill_allowance[order.vt_symbol] -= volume
            if volume <= 0:
                continue

            order.traded += volume
            order.status = Status.ALLTRADED if order.traded >= order.volume else Status.PARTTRADED
            if order.status == Status.ALLTRADED:
                self.active_limit_orders.pop(order.vt_orderid)
            self.on_order(order)

            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=str(len(self.trades) + 1),
                direction=order.direction,
                offset=order.offset,
                price=self.fill_price(order.direction, bid[slot], ask[slot], contract),
                volume=volume,
                datetime=self.clock.now(),
                gateway_name=order.gateway_name,
            )
            self.trades[trade.vt_tradeid] = trade
            if estimated is not None and estimated[slot]:
                self.estimated_trades.add(trade.vt_tradeid)
            self.on_trade(trade)
        self._drop_idle_allowance()

    def _drop_idle_allowance(self) -> None:
        """没有活动委托的合约清掉累计的可成交量，后来的委托从头累计"""
        active: set[str] = {order.vt_symbol for order in self.active_limit_orders.values()}
        self.fill_allowance = {vt_symbol: left for vt_symbol, left in self.fill_allowance.items() if vt_symbol in active}

    def fill_price(self, direction: Direction, bid: float, ask: float, contract: ContractData) -> float:
        """按成交价模型取成交价"""
        opposite, own = (ask, bid) if direction == Direction.LONG else (bid, ask)
        if not np.isfinite(own):
            return float(opposite)

        model: str = get_setting(self.fill_model, contract, "natural")
        if model == "mid":
            return float((bid + ask) / 2)
        if model == "spread_ratio":
            return float(own + (opposite - own) * get_setting(self.fill_ratio, contract, 0.5))
        return float(opposite)


class BarSnapshotSource(TickSnapshotSource):
    """BAR 模式的快照来源"""

    def __init__(self, clock: Clock, day: MinuteDay, spread_ticks: np.ndarray) -> None:
        super().__init__(clock, day.slots)
        self.day: MinuteDay = day
        pricetick: np.ndarray = np.array([c.pricetick for c in day.contracts])
        self.below: np.ndarray = np.floor(spread_ticks / 2) * pricetick   # 估算买价在收盘价下方的距离，奇数档时卖价一侧多一档
        self.spread: np.ndarray = spread_ticks * pricetick
        self.columns["est_quote"] = np.zeros(len(day.slots))
        self.greeks: bool = False

    def update_minute(self, m: int) -> None:
        day_columns: dict[str, np.ndarray] = self.day.columns
        columns: dict[str, np.ndarray] = self.columns
        traded: np.ndarray = self.day.traded(m)
        quoted: np.ndarray = (day_columns["flags"][m] & CacheFlag.NO_QUOTE) == 0
        estimated: np.ndarray = traded & ~quoted
        close: np.ndarray = day_columns["close"][m]
        estimated_bid: np.ndarray = close - self.below

        columns["last"] = np.where(traded, close, columns["last"])
        columns["open_interest"] = np.where(traded, day_columns["open_interest"][m], columns["open_interest"])
        for name in ("volume", "turnover"):
            columns[name] = np.where(traded, np.nan_to_num(columns[name]) + day_columns[name][m], columns[name])

        real: np.ndarray = columns["est_quote"] == 0
        kept_bid: np.ndarray = np.where(real, columns["bid1"], np.nan)
        kept_ask: np.ndarray = np.where(real, columns["ask1"], np.nan)
        bid: np.ndarray = np.where(quoted, day_columns["bid1"][m], np.where(estimated, estimated_bid, kept_bid))
        columns["bid1"] = np.where(bid > 0, bid, np.nan)
        columns["ask1"] = np.where(quoted, day_columns["ask1"][m], np.where(estimated, estimated_bid + self.spread, kept_ask))
        for name in ("bid_vol1", "ask_vol1"):
            columns[name] = np.where(quoted, day_columns[name][m], columns[name])
        columns["est_quote"] = np.where(quoted, 0.0, np.where(estimated, 1.0, columns["est_quote"]))
        if self.greeks:
            for name in GREEK_COLUMNS:
                columns[name] = day_columns[name][m]
        self.dirty = True


class TickFeed:
    """TICK 模式回放的行情流"""

    def __init__(self, load: Callable[[str], Sequence[TickData]]) -> None:
        self.load: Callable[[str], Sequence[TickData]] = load
        self.watched: set[str] = set()
        self.heap: list[tuple[datetime, int, int, Sequence[TickData], TickData]] = []
        self.count: int = 0

    def watch(self, vt_symbols: Iterable[str], now: datetime) -> None:
        """开始推送这些合约"""
        for vt_symbol in sorted(vt_symbols):
            if vt_symbol in self.watched:
                continue
            self.watched.add(vt_symbol)
            ticks: Sequence[TickData] = self.load(vt_symbol)
            index: int = max(bisect_right(ticks, now, key=lambda tick: naive_time(tick.datetime)) - 1, 0)
            if index < len(ticks):
                tick: TickData = ticks[index]
                self.push(ticks, index, tick, max(now, naive_time(tick.datetime)))

    def push(self, ticks: Sequence[TickData], index: int, tick: TickData, moment: datetime) -> None:
        heapq.heappush(self.heap, (moment, self.count, index, ticks, tick))
        self.count += 1

    def next(self) -> tuple[datetime, TickData] | None:
        """下一笔 tick 与回放时刻"""
        if not self.heap:
            return None
        moment, _, index, ticks, tick = heapq.heappop(self.heap)
        if index + 1 < len(ticks):
            following: TickData = ticks[index + 1]
            self.push(ticks, index + 1, following, naive_time(following.datetime))
        return moment, tick


EXPIRY_ORDERID: str = "EXPIRY"


class ReplayClock:
    """回放时钟"""

    def __init__(self) -> None:
        self.current: datetime = datetime(1970, 1, 1)

    def now(self) -> datetime:
        return self.current


class BacktestingEngine(StrategyEngineBase):
    """期权策略回测引擎"""

    engine_type: EngineType = EngineType.BACKTESTING
    gateway_name: str = "BACKTESTING"

    def __init__(self) -> None:
        """构造函数"""
        super().__init__()
        self.parameters: dict = {}
        self.master_contracts: list[ContractData] = []
        self.last_days: dict[str, date] = {}
        self.start: date
        self.end: date

        self.rate: float | dict[str, float] | None = None
        self.slippage: float | dict[str, float] = 0
        self.fill_model: str | dict[str, str] = "natural"
        self.fill_ratio: float | dict[str, float] = 0.5
        self.fill_volume_ratio: float | dict[str, float] = 0
        self.est_spread_ticks: int | dict[str, int] = 2
        self.mode: BacktestingMode = BacktestingMode.BAR

        self.capital: float = 1_000_000
        self.risk_free: float = 0
        self.annual_days: int = 240

        self.strategy_class: type[StrategyTemplate]
        self.strategy_setting: dict
        self.strategy: StrategyTemplate

        self.clock: ReplayClock = ReplayClock()
        self.source: BarSnapshotSource | TickSnapshotSource
        self.bar_generator: OptionBarGenerator
        self.feed: TickFeed
        self.channel: BacktestingChannel
        self.history: HistoryProvider = HistoryProvider(get_database(), get_datafeed(), self.output)

        self.contracts: dict[str, ContractData] = {}
        self.history_data: dict[date, MinuteDay] = {}
        self.replay_days: list[date] = []
        self.cache: MinuteCache
        self.tick_cache: TickCache
        self.cache_enabled: bool = False
        self.cache_write: bool = True
        self.greeks_buffer: dict[str, np.ndarray] = {}
        self.trade_days: dict[str, date] = {}
        self.trade_costs: dict[str, tuple[float, float]] = {}
        self.expiry_trades: dict[str, TradeData] = {}
        self.cash: float = 0.0

        self.logs: list = []

        self.daily_results: dict[date, PortfolioDailyResult] = {}
        self.daily_df: DataFrame | None = None

        self.load_profiles()

    def set_parameters(
        self,
        contracts: list[ContractData],
        start: date,
        end: date,
        rate: float | dict[str, float] | None = None,
        slippage: float | dict[str, float] = 0,
        capital: float = 1_000_000,
        fill_model: str | dict[str, str] = "natural",
        fill_ratio: float | dict[str, float] = 0.5,
        fill_volume_ratio: float | dict[str, float] = 0,
        est_spread_ticks: int | dict[str, int] = 2,
        risk_free: float = 0,
        annual_days: int = 240,
        rate_table: dict[date, float] | None = None,
        cache: bool = False,
        memory: bool = False,
        cache_path: str = "",
        mode: BacktestingMode = BacktestingMode.BAR,
    ) -> None:
        """设置回测参数"""
        self.parameters = dict(locals())
        del self.parameters["self"]
        known: set[str] = {c.vt_symbol for c in contracts} | {c.option_portfolio for c in contracts if c.option_portfolio}
        for name in ("rate", "slippage", "fill_model", "fill_ratio", "fill_volume_ratio", "est_spread_ticks"):
            value = self.parameters[name]
            if isinstance(value, dict) and (unknown := set(value) - known):
                raise ValueError(f"回测参数 {name} 里有不认识的键（应为合约的 vt_symbol 或期权组合名）：{'、'.join(sorted(unknown))}")

        self.master_contracts = contracts
        self.last_days = {}
        self.start = start
        self.end = end

        self.rate = rate
        self.slippage = slippage
        self.fill_model = fill_model
        self.fill_ratio = fill_ratio
        self.fill_volume_ratio = fill_volume_ratio
        self.est_spread_ticks = est_spread_ticks
        self.rate_table = rate_table or {}
        self.cache = MinuteCache(Path(cache_path) if cache_path else default_cache_path(), memory)
        self.tick_cache = TickCache(self.cache.path / "tick", memory)
        self.cache_enabled = cache
        self.mode = mode
        self.history.cache = self.cache if cache else None

        self.capital = capital
        self.risk_free = risk_free
        self.annual_days = annual_days

    def add_strategy(self, strategy_class: type[StrategyTemplate], setting: dict) -> None:
        """增加策略"""
        self.strategy_class = strategy_class
        self.strategy_setting = setting
        self.strategy = strategy_class(self, strategy_class.__name__, self.gateway_name, setting)
        self.strategies = {self.strategy.strategy_name: self.strategy}

    def contracts_of(self, trading_day: date) -> list[ContractData]:
        """当天的合约主档：已上市，且没过到期日与最后行权日中较晚的一天"""
        return [
            c for c in self.master_contracts
            if (c.option_listed is None or c.option_listed.date() <= trading_day) and self.last_day_of(c) >= trading_day
        ]

    def last_day_of(self, contract: ContractData) -> date:
        """合约留在主档里的最后一天；最后行权日晚于到期日时留到行权日，持仓才能在那天结算"""
        if contract.option_expiry is None:
            return date.max
        last: date | None = self.last_days.get(contract.vt_symbol)
        if last is None:
            profile: MarketProfile | None = route_profiles([contract], self.profiles).get(contract.vt_symbol)
            exercise: date | None = profile.contract_info.contract_attributes(contract).last_exercise_date if profile else None
            last = self.last_days[contract.vt_symbol] = max(contract.option_expiry.date(), exercise or date.min)
        return last

    def routed_contracts_of(self, trading_day: date) -> list[ContractData]:
        """当天有规则实现认领的合约主档"""
        master: list[ContractData] = self.contracts_of(trading_day)
        routed: dict[str, MarketProfile] = route_profiles(master, self.profiles)
        return [c for c in master if c.vt_symbol in routed]

    def load_data(self) -> None:
        """加载历史数据"""
        self.output("开始加载历史数据")

        if self.start > self.end:
            self.output("起始日期不能晚于结束日期")
            return

        self.history_data.clear()
        days: list[date] = [self.start + timedelta(n) for n in range((self.end - self.start).days + 1)]
        days = [day for day in days if any(is_trading_day(p.calendar, day) for p in self.profiles)]
        if self.mode == BacktestingMode.TICK:
            self.replay_days = days
            self.output(f"TICK 模式按策略的关注范围逐合约取数，交易日{len(days)}个")
            return

        cached: set[date] = set(self.cache.days()) if self.cache_enabled else set()
        loaded: int = 0
        for day in days:
            if day in cached:
                continue
            data: MinuteDay | None = self.load_database_day(day)
            if data is None:
                continue
            loaded += 1
            if self.cache_enabled and self.cache_write:
                self.cache.save(data)
                cached.add(day)
            else:
                self.history_data[day] = data

        self.replay_days = [day for day in days if day in cached or day in self.history_data]
        gaps: list[date] = [day for day in days if day not in self.replay_days]
        if gaps:
            self.output("以下交易日没有数据，跳过：{}".format("、".join(day.isoformat() for day in gaps)))
        self.output(f"历史数据加载完成，交易日{len(self.replay_days)}个，其中从数据库取{loaded}个")

    def load_database_day(self, day: date) -> MinuteDay | None:
        """从数据库取一天的分钟线"""
        master: list[ContractData] = self.contracts_of(day)
        routed: dict[str, MarketProfile] = route_profiles(master, self.profiles)
        starts: dict[str, datetime] = {
            p.name: datetime.combine(previous_trading_day(p.calendar, day), time(), DB_TZ) for p in self.profiles
        }
        end: datetime = datetime.combine(day + timedelta(days=1), time(), DB_TZ)
        bars: list[BarData] = []
        for contract in master:
            profile: MarketProfile | None = routed.get(contract.vt_symbol)
            if profile is None:
                continue
            bars += [
                bar for bar in self.history.database.load_bar_data(
                    contract.symbol, contract.exchange, Interval.MINUTE, starts[profile.name], end
                )
                if profile.calendar.trading_day_of(naive_time(bar.datetime)) == day
            ]
        if not bars:
            return None
        return MinuteDay.from_bars(day, [c for c in master if c.vt_symbol in routed], bars)

    def day_ticks(
        self, vt_symbol: str, day: date, cached: TickDay | None, fetched: dict[str, list[TickData]]
    ) -> Sequence[TickData]:
        """某合约当天的 tick"""
        if cached and vt_symbol in cached.covered:
            return cached.ticks_of(vt_symbol)
        fetched[vt_symbol] = self.load_ticks(vt_symbol, day)
        return fetched[vt_symbol]

    def load_ticks(self, vt_symbol: str, day: date) -> list[TickData]:
        """从数据库取某合约当天的 tick"""
        contract: ContractData = self.contracts[vt_symbol]
        calendar: Calendar = self.manager.profile_of[vt_symbol].calendar
        start: datetime = datetime.combine(previous_trading_day(calendar, day), time(), DB_TZ)
        end: datetime = datetime.combine(day + timedelta(days=1), time(), DB_TZ)
        ticks: list[TickData] = self.history.database.load_tick_data(contract.symbol, contract.exchange, start, end)
        return [tick for tick in ticks if calendar.trading_day_of(naive_time(tick.datetime)) == day]

    def run_backtesting(self) -> bool:
        """开始回测"""
        self.clear_state()
        self.add_strategy(self.strategy_class, self.strategy_setting)
        self.channel = BacktestingChannel(
            self.clock, self.process_order, self.process_trade, self.fill_model, self.fill_ratio, self.available_funds,
            self.fill_volume_ratio,
        )
        self.cash = 0.0
        self.trade_days.clear()
        self.trade_costs.clear()
        self.expiry_trades.clear()
        self.contracts.clear()
        self.daily_results.clear()
        self.daily_df = None

        for day in self.replay_days:
            try:
                if self.mode == BacktestingMode.TICK:
                    self.new_tick_day(day)
                else:
                    self.new_day(day)
            except Exception:
                self.output("触发异常，回测终止")
                self.output(traceback.format_exc())
                return False

        self.output("历史数据回放结束")
        return True

    def start_replay_day(self, contracts: list[ContractData], day: date) -> None:
        """重建当日状态"""
        self.contracts.update({c.vt_symbol: c for c in contracts})
        first_day: bool = self.manager is None
        self.start_day(contracts, day)
        if first_day:
            self.init_strategy()

    def init_strategy(self) -> None:
        """初始化策略"""
        self.register_strategy(self.strategy)
        self.call_strategy_func(self.strategy, self.strategy.on_init)
        self.stop_on_error()
        self.history.release()
        self.strategy.inited = True
        self.output("策略初始化完成")

        self.call_strategy_func(self.strategy, self.strategy.on_start)
        self.strategy.trading = True
        self.output("开始回放历史数据")

    def new_day(self, day: date) -> None:
        """回放一个交易日"""
        from_cache: bool = day not in self.history_data
        data: MinuteDay | None = self.cache.load(day) if from_cache else self.history_data[day]
        if data is None:
            self.output(f"{day}：缓存文件读取失败，已改名为 .bad，改从数据库取")
        elif from_cache and not same_contracts(data.contracts, self.routed_contracts_of(day)):
            self.output(f"{day}：缓存里的合约主档与当前合约列表不一致，改从数据库取")
            data = None
        if data is None:
            data = self.load_database_day(day)
            if data is None:
                self.output(f"{day}：数据库也没有数据，跳过")
                return
        spread_ticks: np.ndarray = np.array([get_setting(self.est_spread_ticks, c, 2) for c in data.contracts])
        self.source = BarSnapshotSource(self.clock, data, spread_ticks)
        self.clock.current = data.minutes[0]
        self.start_replay_day(data.contracts, day)

        fingerprint: str = self.greeks_fingerprint()
        self.source.greeks = from_cache and data.fingerprint == fingerprint
        self.greeks_buffer = {}
        if self.cache_enabled and self.cache_write and not self.source.greeks:
            shape: tuple[int, int] = (len(data.minutes), len(data.slots))
            self.greeks_buffer = {name: np.full(shape, np.nan) for name in GREEK_COLUMNS}
            if from_cache and data.fingerprint:
                self.output(f"{day}：缓存里的预算希腊值与当前配置不一致，本次现算并回写")

        for m, minute in enumerate(data.minutes):
            self.new_bars(m, minute)
        self.channel.end_day()
        self.settle_expiry()
        settlement: np.ndarray = data.columns["settlement"][-1]
        self.update_daily_close(day, np.where(np.isfinite(settlement), settlement, self.source.columns["last"]))
        if self.greeks_buffer:
            data.columns.update(self.greeks_buffer)
            data.fingerprint = fingerprint
            self.cache.save(data)

    def new_bars(self, m: int, minute: datetime) -> None:
        """回放一分钟"""
        self.clock.current = minute + timedelta(minutes=1)
        self.source.update_minute(m)

        self.channel.cross_limit_order(self.source.columns, self.manager, traded=self.source.day.columns["volume"][m])
        self.step()
        for name, matrix in self.greeks_buffer.items():
            matrix[m] = self.state.slot_arrays[name]
        self.refresh_watched(self.strategy)
        self.on_bars(self.minute_bars(m))
        self.stop_on_error()

    def minute_bars(self, m: int) -> dict[str, BarData]:
        """关注合约本分钟的 K 线"""
        day: MinuteDay = self.source.day
        has_bar: np.ndarray = day.has_bar(m)
        watched: set[str] = set().union(*(book.watched for book in self.books.values()))
        return {
            vt_symbol: day.bar(m, day.slot_index[vt_symbol]) for vt_symbol in watched
            if has_bar[day.slot_index[vt_symbol]]
        }

    def new_tick_day(self, day: date) -> None:
        """TICK 模式回放一个交易日"""
        cached: TickDay | None = self.tick_cache.load(day) if self.cache_enabled else None
        master: list[ContractData] = self.contracts_of(day)
        if cached and not same_contracts(cached.contracts, master):
            self.output(f"{day}：tick 缓存里的合约主档与当前合约列表不一致，改从数据库取")
            cached = None
        contracts: list[ContractData] = self.routed_contracts_of(day)
        self.source = TickSnapshotSource(self.clock, [c.vt_symbol for c in contracts])
        self.bar_generator = OptionBarGenerator(self.on_bars)
        self.clock.current = min(
            trading_day_start(p.calendar, day) for p in self.profiles if is_trading_day(p.calendar, day)
        )
        self.start_replay_day(contracts, day)

        fetched: dict[str, list[TickData]] = {}
        self.feed = TickFeed(lambda vt_symbol: self.day_ticks(vt_symbol, day, cached, fetched))
        self.refresh_watched(self.strategy)
        self.feed.watch(self.books[self.strategy.strategy_name].watched, self.clock.now())

        next_step: datetime = self.clock.now()
        while item := self.feed.next():
            moment, tick = item
            if moment > self.clock.current and self.source.dirty and self.clock.current >= next_step:
                self.tick_step()
                next_step = self.clock.current + timedelta(seconds=INTERVAL_SECONDS)
            self.clock.current = max(self.clock.current, moment)
            self.new_tick(tick)
        if self.source.dirty:
            self.tick_step()

        self.bar_generator.flush()
        self.channel.end_day()
        self.settle_expiry()
        self.update_daily_close(day, self.source.columns["last"])

        if self.cache_enabled and self.cache_write and fetched:
            ticks: list[TickData] = [tick for vt_ticks in fetched.values() for tick in vt_ticks]
            fresh: TickDay = TickDay.from_ticks(day, master, ticks, list(fetched))
            self.tick_cache.save(cached.merge(fresh) if cached else fresh)

    def new_tick(self, tick: TickData) -> None:
        """回放一笔 tick"""
        before: float = self.source.columns["volume"].item(self.source.slot_index[tick.vt_symbol])
        self.source.update_tick(tick)
        self.bar_generator.update_tick(tick)
        traded: float = tick.volume - before if math.isfinite(before) else 0.0   # 当日第一笔的成交量是累计量，不算本段
        self.channel.cross_limit_order(self.source.columns, self.manager, traded, tick.vt_symbol)
        self.push_tick(tick)

    def tick_step(self) -> None:
        """TICK 模式的节拍"""
        self.bar_generator.close_due(self.clock.now())
        self.step()
        self.feed.watch(self.refresh_watched(self.strategy), self.clock.now())
        self.stop_on_error()

    def stop_on_error(self) -> None:
        """策略出错即终止回测"""
        if self.strategy_errors:
            raise RuntimeError("\n".join(self.strategy_errors.values()))

    def process_trade(self, trade: TradeData) -> None:
        """成交记账"""
        self.trade_days[trade.vt_tradeid] = self.state.trading_day
        self.book_cash(trade)
        super().process_trade(trade)

    def book_cash(self, trade: TradeData) -> None:
        """记成交现金流"""
        sign: int = 1 if trade.direction == Direction.LONG else -1
        turnover: float = trade.volume * self.contracts[trade.vt_symbol].size * trade.price
        commission, slippage = self.trade_costs[trade.vt_tradeid] = self.get_costs(trade, turnover)
        self.cash -= sign * turnover + commission + slippage

    def get_costs(self, trade: TradeData, turnover: float) -> tuple[float, float]:
        """一笔成交的手续费与滑点"""
        if trade.orderid == EXPIRY_ORDERID:
            return 0.0, 0.0
        contract: ContractData = self.contracts[trade.vt_symbol]
        slippage: float = trade.volume * contract.size * get_setting(self.slippage, contract, 0)
        rate: float | None = get_setting(self.rate, contract, None)
        if rate is None:
            return self.manager.profile_of[trade.vt_symbol].cost_model.commission(trade), slippage
        return turnover * rate, slippage

    def available_funds(self) -> float:
        """可用资金"""
        pending: float = 0.0
        for book in self.books.values():
            for vt_orderid in book.active_orderids:
                req: OrderRequest = self.order_requests[vt_orderid]
                margins: np.ndarray | None = self.state.slot_arrays.get("long_margin" if req.direction == Direction.LONG else "short_margin")
                if req.offset == Offset.OPEN and margins is not None:
                    pending += float(np.nan_to_num(margins[self.manager.slot_index[req.vt_symbol]])) * self._unbooked(vt_orderid)
        return self.capital + self.cash + self.position_value - self.margin_occupied - pending

    def settle_expiry(self) -> None:
        """到期结算"""
        for settlement, price in self.settle_expiring_positions():
            if settlement.closed_volume:
                size: float = self.contracts[settlement.vt_symbol].size
                self.add_expiry_trade(
                    settlement.vt_symbol, -settlement.closed_volume, Offset.CLOSE,
                    settlement.cash / (settlement.closed_volume * size),
                )
            for vt_symbol, volume in settlement.new_positions.items():
                self.add_expiry_trade(vt_symbol, volume, Offset.OPEN, price)

    def add_expiry_trade(self, vt_symbol: str, volume: int, offset: Offset, price: float) -> None:
        """记一笔到期结算的合成成交"""
        symbol, exchange = extract_vt_symbol(vt_symbol)
        trade: TradeData = TradeData(
            symbol=symbol,
            exchange=exchange,
            orderid=EXPIRY_ORDERID,
            tradeid=f"{EXPIRY_ORDERID}{len(self.expiry_trades) + 1}",
            direction=Direction.LONG if volume > 0 else Direction.SHORT,
            offset=offset,
            price=price,
            volume=abs(volume),
            datetime=self.clock.now(),
            gateway_name=self.gateway_name,
        )
        self.expiry_trades[trade.vt_tradeid] = trade
        self.trade_days[trade.vt_tradeid] = self.state.trading_day
        self.book_cash(trade)

    def update_daily_close(self, day: date, prices: np.ndarray) -> None:
        """记录当日收盘价"""
        previous: dict[str, float] = self.daily_results[max(self.daily_results)].close_prices if self.daily_results else {}
        slots: list[str] = self.source.slots
        close_prices: dict[str, float] = {vt_symbol: previous[vt_symbol] for vt_symbol in slots if vt_symbol in previous}
        finite: np.ndarray = np.flatnonzero(np.isfinite(prices))
        close_prices.update(zip([slots[i] for i in finite], prices[finite].tolist(), strict=True))
        self.daily_results[day] = PortfolioDailyResult(day, close_prices)

    def calculate_result(self) -> DataFrame | None:
        """计算逐日盯市盈亏"""
        self.output("开始计算逐日盯市盈亏")

        trades: list[TradeData] = self.get_all_trades()
        if not trades:
            self.output("成交记录为空，无法计算")
            return None

        self.daily_results = {day: PortfolioDailyResult(day, result.close_prices) for day, result in self.daily_results.items()}
        if not self.daily_results:
            self.output("没有回放完的交易日，无法计算")
            return None
        unfinished: int = 0
        for trade in trades:
            daily_result: PortfolioDailyResult | None = self.daily_results.get(self.trade_days[trade.vt_tradeid])
            if daily_result is None:   # 回放中途终止的那天没有收盘价
                unfinished += 1
                continue
            daily_result.add_trade(trade)
        if unfinished:
            self.output(f"回放中途终止的交易日没有收盘价，当天成交 {unfinished} 笔不计入逐日盈亏")

        sizes: dict[str, float] = {vt_symbol: c.size for vt_symbol, c in self.contracts.items()}
        pre_closes: dict[str, float] = {}
        start_poses: dict[str, float] = {}

        for daily_result in self.daily_results.values():
            daily_result.calculate_pnl(pre_closes, start_poses, sizes, self.trade_costs)

            pre_closes = daily_result.close_prices
            start_poses = daily_result.end_poses

        results: dict = defaultdict(list)

        for daily_result in self.daily_results.values():
            fields: list = [
                "date", "trade_count", "turnover",
                "commission", "slippage", "trading_pnl",
                "holding_pnl", "total_pnl", "net_pnl"
            ]
            for key in fields:
                value = getattr(daily_result, key)
                results[key].append(value)

        self.daily_df = DataFrame.from_dict(results).set_index("date")

        self.output("逐日盯市盈亏计算完成")
        return self.daily_df

    def calculate_rounds(self) -> list[float]:
        """按平仓回合算净盈亏"""
        rounds: list[float] = []
        pos: dict[str, float] = defaultdict(float)
        pnl: dict[str, float] = defaultdict(float)

        for trade in self.get_all_trades():
            vt_symbol: str = trade.vt_symbol
            sign: int = 1 if trade.direction == Direction.LONG else -1
            turnover: float = trade.volume * self.contracts[vt_symbol].size * trade.price
            commission, slippage = self.trade_costs[trade.vt_tradeid]
            cash: float = -(sign * turnover + commission + slippage)

            closing: float = min(trade.volume, abs(pos[vt_symbol])) if pos[vt_symbol] * sign < 0 else 0
            for part in (closing, trade.volume - closing):   # 让持仓穿过 0 的一笔拆成平仓与开仓两段，现金按手数分摊
                if not part:
                    continue
                pnl[vt_symbol] += cash * part / trade.volume
                pos[vt_symbol] += sign * part
                if pos[vt_symbol] == 0:
                    rounds.append(pnl.pop(vt_symbol))
        return rounds

    def add_balance_columns(self, df: DataFrame) -> None:
        """加资金与回撤列"""
        df["balance"] = df["net_pnl"].cumsum() + self.capital
        df["return"] = np.log(df["balance"] / df["balance"].shift(1).fillna(self.capital))   # 首日按起始资金算（vnpy 丢掉首日）
        df["highlevel"] = df["balance"].cummax().clip(lower=self.capital)                      # 回撤的高点从起始资金起算
        df["drawdown"] = df["balance"] - df["highlevel"]
        df["ddpercent"] = df["drawdown"] / df["highlevel"] * 100

    def calculate_statistics(self, df: DataFrame | None = None, output: bool = True) -> dict:
        """计算统计指标"""
        self.output("开始计算策略统计指标")
        df = self.daily_df if df is None else df

        statistics: dict = {key: 0 for key, _, _ in STATISTICS}
        statistics.update(start_date="", end_date="", capital=self.capital)
        if df is not None:
            self.add_balance_columns(df)
            if (df["balance"] > 0).all():
                statistics.update(self._statistics_of(df))
            else:
                self.output("回测中出现爆仓（资金小于等于0），无法计算策略统计指标")

        if output:
            self.output("-" * 30)
            for key, label, fmt in STATISTICS:
                self.output(f"{label}：\t{fmt.format(statistics[key])}")

        for key, value in statistics.items():
            if isinstance(value, float) and np.isnan(value):   # 无穷大保留：没有亏损时的比率排在最前
                statistics[key] = 0
        self.output("策略统计指标计算完成")
        return statistics

    def _statistics_of(self, df: DataFrame) -> dict:
        """各项统计指标"""
        s: dict = {"start_date": df.index[0], "end_date": df.index[-1]}
        total_days: int = len(df)
        s["total_days"] = total_days
        s["profit_days"] = len(df[df["net_pnl"] > 0])
        s["loss_days"] = len(df[df["net_pnl"] < 0])

        s["end_balance"] = df["balance"].iloc[-1]
        s["max_drawdown"] = float(df["drawdown"].min())
        s["max_ddpercent"] = float(df["ddpercent"].min())
        max_drawdown_end = df["drawdown"].idxmin()
        if isinstance(max_drawdown_end, date):
            max_drawdown_start = df["balance"][:max_drawdown_end].idxmax()  # type: ignore
            s["max_drawdown_duration"] = (max_drawdown_end - max_drawdown_start).days

        for name in ("net_pnl", "commission", "slippage", "turnover", "trade_count"):
            total = df[name].sum()
            s[f"total_{name}"] = total
            s[f"daily_{name}"] = total / total_days

        s["total_return"] = (s["end_balance"] / self.capital - 1) * 100
        s["annual_return"] = s["total_return"] / total_days * self.annual_days
        daily_return: float = df["return"].mean() * 100
        return_std: float = df["return"].std() * 100
        s["daily_return"], s["return_std"] = daily_return, return_std

        daily_risk_free: float = self.risk_free * 100 / self.annual_days   # 年化小数折成日收益，与 daily_return 同为百分数
        excess: float = daily_return - daily_risk_free
        s["sharpe_ratio"] = _ratio(excess, return_std) * np.sqrt(self.annual_days)
        s["return_drawdown_ratio"] = _ratio(s["total_net_pnl"], -s["max_drawdown"])
        s["calmar_ratio"] = _ratio(s["annual_return"], -s["max_ddpercent"])
        downside_std: float = np.sqrt((np.minimum(df["return"], 0) ** 2).mean()) * 100
        s["sortino_ratio"] = _ratio(excess, downside_std) * np.sqrt(self.annual_days)

        threshold: float = np.percentile(df["return"], 5)
        s["var_95"] = -threshold * 100
        s["cvar_95"] = -df["return"][df["return"] <= threshold].mean() * 100
        s["omega_ratio"] = _ratio(float(df["return"][df["return"] > 0].sum()), float(-df["return"][df["return"] < 0].sum()))

        rounds: list[float] = self.calculate_rounds()
        wins: list[float] = [pnl for pnl in rounds if pnl > 0]
        defeats: list[float] = [pnl for pnl in rounds if pnl < 0]
        s["round_count"] = len(rounds)
        s["win_rate"] = len(wins) / len(rounds) * 100 if rounds else 0
        s["profit_loss_ratio"] = _ratio(float(np.mean(wins)) if wins else 0.0, float(-np.mean(defeats)) if defeats else 0.0)

        trades: dict = self.channel.trades
        s["estimated_fill_ratio"] = len(self.channel.estimated_trades) / len(trades) * 100 if trades else 0
        return s

    def show_chart(self, df: DataFrame | None = None) -> go.Figure | None:
        """绘制回测图表"""
        if df is None:
            df = self.daily_df

        if df is None:
            return None

        fig = make_subplots(
            rows=4,
            cols=1,
            subplot_titles=["资金", "回撤", "每日盈亏", "盈亏分布"],
            vertical_spacing=0.06
        )

        balance_line = go.Scatter(
            x=df.index,
            y=df["balance"],
            mode="lines",
            name="资金"
        )
        drawdown_scatter = go.Scatter(
            x=df.index,
            y=df["drawdown"],
            fillcolor="red",
            fill='tozeroy',
            mode="lines",
            name="回撤"
        )
        pnl_bar = go.Bar(y=df["net_pnl"], name="每日盈亏")
        pnl_histogram = go.Histogram(x=df["net_pnl"], nbinsx=100, name="天数")

        fig.add_trace(balance_line, row=1, col=1)
        fig.add_trace(drawdown_scatter, row=2, col=1)
        fig.add_trace(pnl_bar, row=3, col=1)
        fig.add_trace(pnl_histogram, row=4, col=1)

        fig.update_layout(height=1000, width=1000)
        return fig

    def get_all_trades(self) -> list[TradeData]:
        """获取所有成交信息"""
        return sorted([*self.channel.trades.values(), *self.expiry_trades.values()], key=lambda trade: trade.datetime)

    def get_all_orders(self) -> list[OrderData]:
        """获取所有委托信息"""
        return list(self.channel.limit_orders.values())

    def get_all_daily_results(self) -> list["PortfolioDailyResult"]:
        """获取所有每日盈亏信息"""
        return list(self.daily_results.values())

    def run_bf_optimization(
        self,
        optimization_setting: OptimizationSetting,
        output: bool = True,
        max_workers: int | None = None
    ) -> list:
        """暴力穷举优化"""
        if not check_optimization_setting(optimization_setting):
            return []

        evaluate_func: Callable = wrap_evaluate(self, optimization_setting.target_name)
        results: list = run_bf_optimization(
            evaluate_func,
            optimization_setting,
            get_target_value,
            max_workers=max_workers,
            output=self.output,
        )

        if output:
            for result in results:
                msg: str = f"参数：{result[0]}, 目标：{result[1]}"
                self.output(msg)

        return results

    def run_ga_optimization(
        self,
        optimization_setting: OptimizationSetting,
        max_workers: int | None = None,
        ngen: int = 30,
        output: bool = True
    ) -> list:
        """遗传算法优化"""
        if not check_optimization_setting(optimization_setting):
            return []

        evaluate_func: Callable = wrap_evaluate(self, optimization_setting.target_name)
        results: list = run_ga_optimization(
            evaluate_func,
            optimization_setting,
            get_target_value,
            max_workers=max_workers,
            ngen=ngen,
            output=self.output
        )

        if output:
            for result in results:
                msg: str = f"参数：{result[0]}, 目标：{result[1]}"
                self.output(msg)

        return results

    def greeks_fingerprint(self) -> str:
        """预算希腊值的配置指纹"""
        config: dict = {
            "risk_free_rate": self.risk_free_rate,
            "rate_table": sorted((day.isoformat(), rate) for day, rate in self.rate_table.items()),
            "stale_seconds": self.stale_seconds,
            "max_spread": self.max_spread,
            "profiles": [(profile.name, profile.version) for profile in self.profiles],
            "greeks": GREEKS_CONVENTION,
            "est_spread_ticks": self.est_spread_ticks,
            "est_quote": "只在成交量大于 0 的那一分钟有效",
        }
        return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def write_log(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """记日志；引擎自己的日志（规则实现加载、拒单、到期结算等）同时打印"""
        if strategy:
            msg = f"{strategy.strategy_name}: {msg}"
        else:
            self.output(msg)
        self.logs.append(f"{self.clock.now()}\t{msg}")

    def output(self, msg: str) -> None:
        """输出回测引擎信息"""
        print(f"{datetime.now()}\t{msg}")

    def send_notification(self, msg: str, strategy: StrategyTemplate | None = None) -> None:
        """通过已配置渠道推送通知"""
        pass

    def sync_strategy_data(self, strategy: StrategyTemplate) -> None:
        """保存策略数据到文件"""
        pass

    def save_strategy_file(self, file_name: str, data: dict) -> None:
        """回测不写策略自定义数据文件"""
        pass

    def load_strategy_file(self, file_name: str) -> dict:
        """回测不读策略自定义数据文件"""
        return {}

    def put_strategy_event(self, strategy: StrategyTemplate) -> None:
        """推送事件更新策略界面"""
        pass


class ContractDailyResult:
    """合约每日盈亏结果"""

    def __init__(self, result_date: date, close_price: float) -> None:
        """构造函数"""
        self.date: date = result_date
        self.close_price: float = close_price
        self.pre_close: float = 0

        self.trades: list[TradeData] = []
        self.trade_count: int = 0

        self.start_pos: float = 0
        self.end_pos: float = 0

        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0

        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        """添加成交信息"""
        self.trades.append(trade)

    def calculate_pnl(
        self,
        pre_close: float,
        start_pos: float,
        size: float,
        costs: dict[str, tuple[float, float]],
    ) -> None:
        """计算盈亏"""
        self.pre_close = pre_close

        self.start_pos = start_pos
        self.end_pos = start_pos

        self.holding_pnl = self.start_pos * (self.close_price - self.pre_close) * size

        self.trade_count = len(self.trades)

        for trade in self.trades:
            pos_change = trade.volume if trade.direction == Direction.LONG else -trade.volume

            self.end_pos += pos_change

            turnover: float = trade.volume * size * trade.price

            self.trading_pnl += pos_change * (self.close_price - trade.price) * size
            commission, slippage = costs[trade.vt_tradeid]
            self.slippage += slippage
            self.turnover += turnover
            self.commission += commission

        self.total_pnl = self.trading_pnl + self.holding_pnl
        self.net_pnl = self.total_pnl - self.commission - self.slippage


class PortfolioDailyResult:
    """组合每日盈亏结果"""

    def __init__(self, result_date: date, close_prices: dict[str, float]) -> None:
        """构造函数"""
        self.date: date = result_date
        self.close_prices: dict[str, float] = close_prices
        self.end_poses: dict[str, float] = {}

        self.contract_results: dict[str, ContractDailyResult] = {}

        self.trade_count: int = 0
        self.turnover: float = 0
        self.commission: float = 0
        self.slippage: float = 0
        self.trading_pnl: float = 0
        self.holding_pnl: float = 0
        self.total_pnl: float = 0
        self.net_pnl: float = 0

    def add_trade(self, trade: TradeData) -> None:
        """添加成交信息"""
        contract_result: ContractDailyResult | None = self.contract_results.get(trade.vt_symbol)
        if contract_result is None:
            close_price: float = self.close_prices.setdefault(trade.vt_symbol, trade.price)
            contract_result = self.contract_results[trade.vt_symbol] = ContractDailyResult(self.date, close_price)
        contract_result.add_trade(trade)

    def calculate_pnl(
        self,
        pre_closes: dict[str, float],
        start_poses: dict[str, float],
        sizes: dict[str, float],
        costs: dict[str, tuple[float, float]],
    ) -> None:
        """计算盈亏"""
        for vt_symbol, pos in start_poses.items():
            if pos:
                self.close_prices.setdefault(vt_symbol, pre_closes[vt_symbol])
        for vt_symbol, close_price in self.close_prices.items():
            contract_result: ContractDailyResult | None = self.contract_results.get(vt_symbol)
            if contract_result is None:
                if not start_poses.get(vt_symbol):
                    continue
                contract_result = self.contract_results[vt_symbol] = ContractDailyResult(self.date, close_price)
            contract_result.calculate_pnl(
                pre_closes.get(vt_symbol, 0),
                start_poses.get(vt_symbol, 0),
                sizes[vt_symbol],
                costs,
            )

            self.trade_count += contract_result.trade_count
            self.turnover += contract_result.turnover
            self.commission += contract_result.commission
            self.slippage += contract_result.slippage
            self.trading_pnl += contract_result.trading_pnl
            self.holding_pnl += contract_result.holding_pnl
            self.total_pnl += contract_result.total_pnl
            self.net_pnl += contract_result.net_pnl

            self.end_poses[vt_symbol] = contract_result.end_pos


def evaluate(
    target_name: str, strategy_class: type[StrategyTemplate], parameters: dict, engine_setup: dict, setting: dict
) -> tuple:
    """进程池内运行一组回测"""
    engine: BacktestingEngine = BacktestingEngine()
    engine.set_parameters(**{**parameters, "memory": False})
    engine.execution = engine_setup["execution"]
    engine.risk_free_rate = engine_setup["risk_free_rate"]
    engine.stale_seconds = engine_setup["stale_seconds"]
    engine.max_spread = engine_setup["max_spread"]
    engine.profiles = engine_setup["profiles"]
    engine.cache_write = False
    engine.add_strategy(strategy_class, {**engine_setup["setting"], **setting})
    engine.load_data()
    if not engine.run_backtesting():
        return (str(setting), -np.inf, {})
    engine.calculate_result()
    statistics: dict = engine.calculate_statistics(output=False)

    target_value: float = statistics[target_name]
    return (str(setting), target_value, statistics)


def wrap_evaluate(engine: BacktestingEngine, target_name: str) -> Callable:
    """包装回测配置函数以供进程池内运行"""
    engine_setup: dict = {
        "execution": engine.execution, "risk_free_rate": engine.risk_free_rate, "stale_seconds": engine.stale_seconds,
        "max_spread": engine.max_spread, "profiles": engine.profiles, "setting": engine.strategy_setting,
    }
    func: Callable = partial(evaluate, target_name, engine.strategy_class, engine.parameters, engine_setup)
    return func


def get_target_value(result: list) -> float:
    """获取优化目标"""
    target_value: float = result[1]
    return target_value

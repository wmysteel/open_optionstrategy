"""策略模板"""
from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import copy, deepcopy
from datetime import date
from typing import TYPE_CHECKING, Any

from vnpy.trader.constant import Direction, Interval, Offset
from vnpy.trader.object import BarData, OrderData, TickData, TradeData

from .base import STRATEGY_LABELS, EngineType
from .object import OptionData, OptionFilter, PortfolioData, Snapshot

if TYPE_CHECKING:
    from .engine import StrategyEngineBase


class _Declared:
    """声明式参数或变量"""

    __slots__ = ("default", "label", "is_parameter")

    def __init__(self, default: Any, label: str, is_parameter: bool) -> None:
        self.default = default
        self.label = label
        self.is_parameter = is_parameter


def Parameter(default: Any, label: str = "") -> Any:
    """声明策略参数"""
    return _Declared(default, label, True)


def Variable(default: Any, label: str = "") -> Any:
    """声明策略变量"""
    return _Declared(default, label, False)


class StrategyTemplate(ABC):
    """期权策略模板"""

    author: str = ""
    parameters: list = []
    variables: list = []
    labels: Mapping[str, str] = STRATEGY_LABELS

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """收集声明的参数与变量"""
        super().__init_subclass__(**kwargs)
        declared: dict[str, _Declared] = {name: v for name, v in vars(cls).items() if isinstance(v, _Declared)}
        if not declared:
            return
        for name, item in declared.items():
            setattr(cls, name, item.default)
        cls.parameters = [n for n in cls.parameters if n not in declared] + [n for n, d in declared.items() if d.is_parameter]
        cls.variables = [n for n in cls.variables if n not in declared] + [n for n, d in declared.items() if not d.is_parameter]
        cls.labels = {**cls.labels, **{name: item.label for name, item in declared.items() if item.label}}

    def __init__(self, strategy_engine: "StrategyEngineBase", strategy_name: str, gateway_name: str, setting: dict) -> None:
        """构造函数"""
        self.strategy_engine: StrategyEngineBase = strategy_engine
        self.strategy_name: str = strategy_name
        self.gateway_name: str = gateway_name

        self.inited: bool = False
        self.trading: bool = False

        self.variables: list = copy(self.variables)
        self.variables.insert(0, "inited")
        self.variables.insert(1, "trading")
        for name in (*self.parameters, *self.variables):
            if hasattr(type(self), name):   # 类上的默认值每个实例各复制一份，列表、字典才不会被同类实例共用
                setattr(self, name, deepcopy(getattr(type(self), name)))

        self.update_setting(setting)

    def update_setting(self, setting: dict) -> None:
        """设置策略参数"""
        for name in self.parameters:
            if name in setting:
                setattr(self, name, setting[name])

    @classmethod
    def get_class_parameters(cls) -> dict:
        """查取策略默认参数"""
        return {name: getattr(cls, name) for name in cls.parameters}

    def get_parameters(self) -> dict:
        """查询策略参数"""
        return {name: getattr(self, name) for name in self.parameters}

    def get_variables(self) -> dict:
        """查询策略变量"""
        return {name: getattr(self, name) for name in self.variables}

    def get_data(self) -> dict:
        """查询策略状态数据"""
        return {
            "strategy_name": self.strategy_name,
            "gateway_name": self.gateway_name,
            "class_name": self.__class__.__name__,
            "author": self.author,
            "parameters": self.get_parameters(),
            "variables": self.get_variables(),
        }

    @abstractmethod
    def on_init(self) -> None:
        """策略初始化回调"""
        return

    def on_start(self) -> None:
        """策略启动回调"""
        return

    def on_stop(self) -> None:
        """策略停止回调"""
        return

    def on_tick(self, tick: TickData) -> None:
        """行情推送回调"""
        return

    @abstractmethod
    def on_bars(self, bars: dict[str, BarData]) -> None:
        """K 线切片回调"""
        return

    def on_snapshot(self, snapshot: Snapshot) -> None:
        """全市场节拍回调"""
        return

    def on_expiry(self, options: list[OptionData]) -> None:
        """持仓期权到期日回调"""
        return

    def update_trade(self, trade: TradeData) -> None:
        """成交数据更新"""
        return

    def update_order(self, order: OrderData) -> None:
        """委托数据更新"""
        return

    def buy(self, vt_symbol: str, price: float, volume: float) -> list[str]:
        """买入开仓"""
        return self.send_order(vt_symbol, Direction.LONG, Offset.OPEN, price, volume)

    def sell(self, vt_symbol: str, price: float, volume: float) -> list[str]:
        """卖出平仓"""
        return self.send_order(vt_symbol, Direction.SHORT, Offset.CLOSE, price, volume)

    def short(self, vt_symbol: str, price: float, volume: float) -> list[str]:
        """卖出开仓"""
        return self.send_order(vt_symbol, Direction.SHORT, Offset.OPEN, price, volume)

    def cover(self, vt_symbol: str, price: float, volume: float) -> list[str]:
        """买入平仓"""
        return self.send_order(vt_symbol, Direction.LONG, Offset.CLOSE, price, volume)

    def send_order(
        self, vt_symbol: str, direction: Direction, offset: Offset, price: float, volume: float
    ) -> list[str]:
        """发送委托"""
        if not self.trading:
            return []
        return self.strategy_engine.send_order(self, vt_symbol, direction, offset, price, volume)

    def cancel_order(self, vt_orderid: str) -> None:
        """撤销委托"""
        if self.trading:
            self.strategy_engine.cancel_order(self, vt_orderid)

    def cancel_all(self) -> None:
        """全撤活动委托"""
        if self.trading:
            self.strategy_engine.cancel_all(self)

    def set_target(self, vt_symbol: str, target: int) -> None:
        """设置目标仓位"""
        self.strategy_engine.set_target(self, vt_symbol, target)

    def get_target(self, vt_symbol: str) -> int:
        """查询目标仓位"""
        return self.strategy_engine.get_target(self, vt_symbol)

    def clear_targets(self) -> int:
        """清空目标仓位"""
        return self.strategy_engine.clear_targets(self)

    def set_combo_target(self, name: str, legs: dict[str, int], target: int) -> None:
        """设置组合目标"""
        self.strategy_engine.set_combo_target(self, name, legs, target)

    def get_combo_target(self, name: str) -> int:
        """查询组合目标份数"""
        return self.strategy_engine.get_combo_target(self, name)

    def get_greeks(self, portfolio_name: str = "") -> dict[str, float]:
        """持仓 Greeks 合计"""
        return self.strategy_engine.get_greeks(self, portfolio_name)

    def get_combo_pos(self, name: str) -> int:
        """查询组合已建成的份数"""
        return self.strategy_engine.get_combo_pos(self, name)

    def get_combo_greeks(self, legs: dict[str, int]) -> dict[str, float]:
        """一组腿按比例的希腊值合计"""
        return self.strategy_engine.get_combo_greeks(self, legs)

    def get_combo_premium(self, legs: dict[str, int], cross: bool = False) -> float:
        """一组腿按比例的净权利金（元）"""
        return self.strategy_engine.get_combo_premium(self, legs, cross)

    def get_cost(self, vt_symbol: str) -> float:
        """查询本策略该合约持仓的开仓均价"""
        return self.strategy_engine.get_cost(self, vt_symbol)

    def get_open_pnl(self, vt_symbols: list[str] | None = None) -> float:
        """查询本策略这些合约持仓的浮动盈亏（元），不给合约就算全部持仓"""
        return self.strategy_engine.get_open_pnl(self, vt_symbols)

    def get_theo_price(self, vt_symbol: str, vol: float, underlying: float | None = None, days: int = 0) -> float:
        """查询期权按给定波动率的理论价"""
        return self.strategy_engine.get_theo_price(self, vt_symbol, vol, underlying, days)

    def get_combo_margin(self, legs: dict[str, int]) -> float:
        """一组腿的保证金预估（元）"""
        return self.strategy_engine.get_combo_margin(self, legs)

    def execute_trading(self, price_data: dict[str, float], percent_add: float) -> list[str]:
        """按目标仓位调仓"""
        if not self.trading:
            return []
        return self.strategy_engine.execute_trading(self, price_data, percent_add)

    def subscribe_options(self, portfolio_name: str, option_filter: OptionFilter | None = None) -> bool:
        """登记关注范围"""
        return self.strategy_engine.subscribe_options(self, portfolio_name, option_filter or OptionFilter())

    def subscribe_data(self, vt_symbol: str) -> bool:
        """加入关注列表"""
        return self.strategy_engine.subscribe_data(self, vt_symbol)

    def get_portfolio(self, portfolio_name: str) -> PortfolioData | None:
        """查询期权组合"""
        return self.strategy_engine.get_portfolio(self, portfolio_name)

    def get_pos(self, vt_symbol: str) -> int:
        """查询当前持仓"""
        return self.strategy_engine.get_pos(self, vt_symbol)

    def get_price(self, vt_symbol: str) -> float:
        """查询合约最新盯市价"""
        return self.strategy_engine.get_price(self, vt_symbol)

    def get_pnl_today(self) -> float:
        """查询本策略当日盈亏"""
        return self.strategy_engine.get_pnl_today(self)

    def get_margin(self) -> float:
        """查询本策略保证金占用"""
        return self.strategy_engine.get_margin(self)

    def get_available(self) -> float | None:
        """查询账户可用资金"""
        return self.strategy_engine.get_available(self)

    def get_order(self, vt_orderid: str) -> OrderData | None:
        """查询委托数据"""
        return self.strategy_engine.get_order(vt_orderid)

    def get_all_active_orderids(self) -> list[str]:
        """获取全部活动状态的委托号"""
        return self.strategy_engine.get_all_active_orderids(self)

    def load_bars(self, vt_symbol: str, days: int, interval: Interval = Interval.MINUTE) -> None:
        """加载历史 K 线"""
        self.strategy_engine.load_bars(self, vt_symbol, days, interval)

    def get_today(self) -> date | None:
        """查询当前交易日"""
        return self.strategy_engine.get_today()

    def get_engine_type(self) -> EngineType:
        """查询引擎类型"""
        return self.strategy_engine.get_engine_type()

    def get_pricetick(self, vt_symbol: str) -> float | None:
        """查询合约最小价格跳动"""
        return self.strategy_engine.get_pricetick(self, vt_symbol)

    def get_size(self, vt_symbol: str) -> float | None:
        """查询合约乘数"""
        return self.strategy_engine.get_size(self, vt_symbol)

    def write_log(self, msg: str) -> None:
        """记录日志"""
        self.strategy_engine.write_log(msg, self)

    def put_event(self) -> None:
        """推送策略数据更新事件"""
        if self.inited:
            self.strategy_engine.put_strategy_event(self)

    def send_notification(self, msg: str) -> None:
        """通过已配置渠道推送通知"""
        if self.inited:
            self.strategy_engine.send_notification(msg, self)

    send_email = send_notification

    def sync_data(self) -> None:
        """同步策略状态数据到文件"""
        if self.trading:
            self.strategy_engine.sync_strategy_data(self)

    def save_data(self, file_name: str, data: dict) -> None:
        """保存自定义数据到 json 文件"""
        self.strategy_engine.save_strategy_file(file_name, data)

    def load_data(self, file_name: str) -> dict:
        """读取自定义数据 json 文件"""
        return self.strategy_engine.load_strategy_file(file_name)

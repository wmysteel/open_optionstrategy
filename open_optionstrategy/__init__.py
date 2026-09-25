"""基于 VeighNa 的开源期权策略应用"""
from pathlib import Path

from vnpy.trader.app import BaseApp
from vnpy.trader.constant import Direction, Offset
from vnpy.trader.object import BarData, OrderData, TickData, TradeData

from .base import APP_NAME
from .engine import OptionStrategyEngine
from .template import StrategyTemplate

__all__ = [
    "APP_NAME",
    "OptionStrategyEngine",
    "StrategyTemplate",
    "Direction",
    "Offset",
    "TickData",
    "BarData",
    "TradeData",
    "OrderData",
    "OptionStrategyApp",
]


__version__ = "0.1.0"


class OptionStrategyApp(BaseApp):
    """期权策略应用入口"""

    app_name: str = APP_NAME
    app_module: str = __module__
    app_path: Path = Path(__file__).parent
    display_name: str = "期权策略"
    engine_class: type[OptionStrategyEngine] = OptionStrategyEngine
    widget_name: str = "OptionStrategyManager"
    icon_name: str = str(app_path.joinpath("ui", "option.ico"))

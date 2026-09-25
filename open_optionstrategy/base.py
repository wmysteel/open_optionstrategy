"""常量与枚举"""
from collections.abc import Mapping
from enum import Enum, IntFlag
from types import MappingProxyType

APP_NAME = "OpenOptionStrategy"


class EngineType(Enum):
    LIVE = "实盘"
    BACKTESTING = "回测"


EVENT_OPTION_LOG = "eOpenOptionLog"
EVENT_OPTION_STRATEGY = "eOpenOptionStrategy"

INTERVAL_SECONDS: float = 0.5


class BacktestingMode(Enum):
    """回测模式"""

    BAR = 1
    TICK = 2


class ExerciseStyle(Enum):
    EUROPEAN = "欧式"
    AMERICAN = "美式"


class DayType(Enum):
    """剩余天数的口径"""

    CALENDAR = "自然日"
    TRADING = "交易日"


class StrikeRangeKind(Enum):
    """行权价范围的写法"""

    FIXED = "固定行权价"
    ATM_RELATIVE = "平值上下若干档"
    ATM_PERCENT = "标的价上下百分比"
    DELTA = "单位 delta 区间"


class Quality(IntFlag):
    """槽位质量位"""

    NONE = 0
    HAS_BID = 1
    HAS_ASK = 2
    STALE_PRICE = 4
    IV_INVALID = 8
    LOW_VEGA = 16


class CacheFlag(IntFlag):
    """分钟缓存标记位"""

    NONE = 0
    NO_QUOTE = 2


GREEKS_CONVENTION: dict[str, str] = {
    "delta": "每张合约，已乘合约乘数，标的价格变动 1 元时的盈亏",
    "gamma": "每张合约，已乘合约乘数，标的价格变动 1 元时 delta 的变化",
    "vega": "每张合约，已乘合约乘数，波动率变动 1 个百分点时的盈亏",
    "theta": "每张合约，已乘合约乘数，每过一个自然日的盈亏",
    "iv": "小数，界面显示为百分数",
}


GREEK_COLUMNS: tuple[str, ...] = ("iv", "bid_iv", "ask_iv", "delta", "gamma", "vega", "theta")
GREEK_NAMES: tuple[str, ...] = ("delta", "gamma", "vega", "theta")


STRATEGY_LABELS: Mapping[str, str] = MappingProxyType({
    "inited": "已初始化",
    "trading": "交易中",
    "strategy_name": "策略名",
    "gateway_name": "交易接口",
})

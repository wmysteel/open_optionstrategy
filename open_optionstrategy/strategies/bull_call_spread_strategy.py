"""示例策略：牛市认购价差"""
from vnpy.trader.object import BarData

from open_optionstrategy.object import OptionData, OptionFilter, Snapshot
from open_optionstrategy.template import Parameter, StrategyTemplate, Variable

COMBO = "bull_call"


class BullCallSpreadStrategy(StrategyTemplate):
    """示例：买平值认购、卖高几档认购，到期日平仓后在下一条链上重新开"""

    author = "open_optionstrategy"

    portfolio_name: str = Parameter("", "期权组合")
    min_days: int = Parameter(20, "最少剩余天数")
    max_days: int = Parameter(60, "最多剩余天数")
    width: int = Parameter(2, "价差档数")
    volume: int = Parameter(1, "份数")
    percent_add: float = Parameter(0.0, "超价比例")

    legs: dict = Variable({}, "持仓腿")

    def scope(self) -> OptionFilter:
        """剩余天数窗口内的链"""
        return OptionFilter().days(self.min_days, self.max_days)

    def on_init(self) -> None:
        """登记关注范围"""
        self.subscribe_options(self.portfolio_name, self.scope())

    def on_bars(self, bars: dict[str, BarData]) -> None:
        return

    def on_snapshot(self, snapshot: Snapshot) -> None:
        """选腿开仓，每个节拍按中间价调仓"""
        if not self.trading:
            return
        portfolio = self.get_portfolio(self.portfolio_name)
        if portfolio is None:
            return
        if not self.legs:
            chains = self.scope().chains(portfolio)
            low = chains[0].get_option_by_level(1, 0) if chains else None
            high = chains[0].get_option_by_level(1, self.width) if chains else None
            if low is None or high is None:
                return
            self.legs = {low.vt_symbol: 1, high.vt_symbol: -1}
            self.set_combo_target(COMBO, self.legs, self.volume)
            self.put_event()
        options = [portfolio.options[leg] for leg in self.legs if leg in portfolio.options]
        if len(options) < len(self.legs):
            self.write_log("持仓腿今日不在组合中，重新选腿")
            self.clear_targets()
            self.legs = {}
            self.put_event()
            return
        if self.get_combo_target(COMBO) == 0 and all(o.pos == 0 for o in options):
            self.legs = {}
            self.put_event()
            return
        self.execute_trading({o.vt_symbol: o.mid for o in options}, self.percent_add)

    def on_expiry(self, options: list[OptionData]) -> None:
        """到期日平仓"""
        if any(o.vt_symbol in self.legs for o in options):
            self.set_combo_target(COMBO, self.legs, 0)

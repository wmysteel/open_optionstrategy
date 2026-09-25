"""示例策略：认购蝶式"""
from vnpy.trader.object import BarData

from open_optionstrategy.object import OptionFilter, Snapshot
from open_optionstrategy.template import Parameter, StrategyTemplate, Variable

COMBO = "butterfly"


class ButterflyStrategy(StrategyTemplate):
    """示例：买低几档与高几档认购、卖 2 张平值认购，临近到期平仓（没有买价的翼留到到期作废）后在下一条链上重新开"""

    author = "open_optionstrategy"

    portfolio_name: str = Parameter("", "期权组合")
    min_days: int = Parameter(20, "最少剩余天数")
    max_days: int = Parameter(60, "最多剩余天数")
    wing: int = Parameter(2, "翼宽档数")
    volume: int = Parameter(1, "份数")
    exit_days: int = Parameter(5, "剩余几天平仓")
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
        """选腿开仓，临近到期平仓，每个节拍按中间价调仓"""
        if not self.trading:
            return
        portfolio = self.get_portfolio(self.portfolio_name)
        if portfolio is None:
            return
        if not self.legs:
            chains = self.scope().chains(portfolio)
            low, middle, high = (chains[0].get_option_by_level(1, level) if chains else None for level in (-self.wing, 0, self.wing))
            if low is None or middle is None or high is None:
                return
            self.legs = {low.vt_symbol: 1, middle.vt_symbol: -2, high.vt_symbol: 1}
            self.set_combo_target(COMBO, self.legs, self.volume)
            self.put_event()
        options = [portfolio.options[leg] for leg in self.legs if leg in portfolio.options]
        if len(options) < len(self.legs):
            self.write_log("持仓腿今日不在组合中，重新选腿")
            self.clear_targets()
            self.legs = {}
            self.put_event()
            return
        if options[0].chain.days_to_expiry() <= self.exit_days:
            closing = [o for o in options if self.legs[o.vt_symbol] < 0 or o.bid >= o.contract.pricetick]    # 没有买价的翼卖不出去，留到到期作废
            self.set_combo_target(COMBO, {o.vt_symbol: self.legs[o.vt_symbol] for o in closing}, 0)
            if all(o.pos == 0 for o in closing):
                self.legs = {}
                self.put_event()
                return
        self.execute_trading({o.vt_symbol: o.mid for o in options}, self.percent_add)

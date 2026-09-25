"""示例策略：铁鹰"""
from vnpy.trader.object import BarData

from open_optionstrategy.object import ChainData, OptionFilter, Snapshot, StrikeRange
from open_optionstrategy.template import Parameter, StrategyTemplate, Variable

COMBO = "iron_condor"


class IronCondorStrategy(StrategyTemplate):
    """示例：按 delta 卖虚值认购与认沽、买更虚的保护，临近到期平仓（没有买价的保护腿留到到期作废）后在下一条链上重新开"""

    author = "open_optionstrategy"

    portfolio_name: str = Parameter("", "期权组合")
    min_days: int = Parameter(20, "最少剩余天数")
    max_days: int = Parameter(60, "最多剩余天数")
    short_delta: float = Parameter(0.25, "卖出腿delta")
    wing_delta: float = Parameter(0.10, "保护腿delta")
    volume: int = Parameter(1, "份数")
    exit_days: int = Parameter(5, "剩余几天平仓")
    percent_add: float = Parameter(0.0, "超价比例")

    legs: dict = Variable({}, "持仓腿")

    def scope(self) -> OptionFilter:
        """剩余天数窗口内、单位 delta 在保护腿与卖出腿之间（两边各放宽 0.05）的期权"""
        center = (self.short_delta + self.wing_delta) / 2
        return OptionFilter().days(self.min_days, self.max_days).strikes(StrikeRange.delta(center, center - self.wing_delta + 0.05))

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
            legs = self.pick_legs(chains[0]) if chains else {}
            if not legs:
                return
            self.legs = legs
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
            closing = [o for o in options if self.legs[o.vt_symbol] < 0 or o.bid >= o.contract.pricetick]    # 没有买价的保护腿卖不出去，留到到期作废
            self.set_combo_target(COMBO, {o.vt_symbol: self.legs[o.vt_symbol] for o in closing}, 0)
            if all(o.pos == 0 for o in closing):
                self.legs = {}
                self.put_event()
                return
        self.execute_trading({o.vt_symbol: o.mid for o in options}, self.percent_add)

    def pick_legs(self, chain: ChainData) -> dict[str, int]:
        """四条腿：卖出腿与保护腿各按单位 delta 选，保护腿须比卖出腿更虚；行情没到齐时返回空"""
        short_call, wing_call = chain.select_by_delta(1, self.short_delta), chain.select_by_delta(1, self.wing_delta)
        short_put, wing_put = chain.select_by_delta(-1, self.short_delta), chain.select_by_delta(-1, self.wing_delta)
        if short_call is None or wing_call is None or short_put is None or wing_put is None:
            return {}
        if not (wing_put.strike < short_put.strike < short_call.strike < wing_call.strike):
            return {}
        return {short_call.vt_symbol: -1, wing_call.vt_symbol: 1, short_put.vt_symbol: -1, wing_put.vt_symbol: 1}

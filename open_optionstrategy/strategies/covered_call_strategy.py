"""示例策略：备兑开仓，临近到期换到下一条链"""
from vnpy.trader.object import BarData

from open_optionstrategy.object import OptionFilter, PortfolioData, Snapshot
from open_optionstrategy.template import Parameter, StrategyTemplate, Variable


class CoveredCallStrategy(StrategyTemplate):
    """示例：持有标的期货、卖虚值认购；剩余天数不多时把认购换到下一条链，标的期货换月时一起换"""

    author = "open_optionstrategy"

    portfolio_name: str = Parameter("", "期权组合")
    min_days: int = Parameter(20, "最少剩余天数")
    max_days: int = Parameter(60, "最多剩余天数")
    level: int = Parameter(2, "虚值档数")
    volume: int = Parameter(1, "手数")
    roll_days: int = Parameter(5, "剩余几天换月")
    percent_add: float = Parameter(0.0, "超价比例")

    call: str = Variable("", "卖出的认购")
    future: str = Variable("", "持有的期货")
    closing: list = Variable([], "平仓中的合约")

    def scope(self) -> OptionFilter:
        """剩余天数窗口内的链"""
        return OptionFilter().days(self.min_days, self.max_days)

    def on_init(self) -> None:
        """登记关注范围"""
        self.subscribe_options(self.portfolio_name, self.scope())

    def on_bars(self, bars: dict[str, BarData]) -> None:
        return

    def on_snapshot(self, snapshot: Snapshot) -> None:
        """开仓或换月，每个节拍按中间价调仓"""
        if not self.trading:
            return
        portfolio = self.get_portfolio(self.portfolio_name)
        if portfolio is None:
            return
        current = portfolio.options.get(self.call)
        if self.call and current is None:
            self.write_log(f"认购{self.call}今日不在组合中，重新选择")
            self.clear_targets()
            self.call = ""
        if current is None or current.chain.days_to_expiry() <= self.roll_days:
            self.roll(portfolio)
        self.closing = [s for s in self.closing if self.get_pos(s) != 0]
        prices = {
            s: portfolio.options[s].mid if s in portfolio.options else self.get_price(s)
            for s in [self.call, self.future, *self.closing] if s
        }
        self.execute_trading(prices, self.percent_add)

    def roll(self, portfolio: PortfolioData) -> None:
        """换到剩余天数多于换月天数的最近一条链：旧认购平掉，标的期货不同就一起换"""
        chains = [c for c in self.scope().chains(portfolio) if c.days_to_expiry() > self.roll_days]
        call = chains[0].get_option_by_level(1, self.level) if chains else None
        if call is None or call.vt_symbol == self.call:
            return
        future = f"{chains[0].underlying_symbol}.{chains[0].exchange.value}"
        if self.call:
            self.set_target(self.call, 0)
            self.closing = [*self.closing, self.call]
        if self.future and self.future != future:
            self.set_target(self.future, 0)
            self.closing = [*self.closing, self.future]
        self.call, self.future = call.vt_symbol, future
        self.set_target(self.future, self.volume)
        self.set_target(self.call, -self.volume)
        self.write_log(f"卖出认购 {self.call}，持有期货 {self.future}")
        self.put_event()

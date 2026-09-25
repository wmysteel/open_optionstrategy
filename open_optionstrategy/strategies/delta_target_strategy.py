"""示例策略"""
from vnpy.trader.object import BarData

from open_optionstrategy.object import OptionFilter, Snapshot
from open_optionstrategy.template import Parameter, StrategyTemplate, Variable


class DeltaTargetStrategy(StrategyTemplate):
    """示例：按 delta 选认购并调仓"""

    author = "open_optionstrategy"

    portfolio_name: str = Parameter("", "期权组合")
    min_days: int = Parameter(5, "最少剩余天数")
    max_days: int = Parameter(45, "最多剩余天数")
    target_delta: float = Parameter(0.25, "目标delta")
    target_volume: int = Parameter(-1, "目标手数")
    percent_add: float = Parameter(0.0, "超价比例")
    preload_days: int = Parameter(3, "预热天数")

    target_symbol: str = Variable("", "目标合约")

    def scope(self) -> OptionFilter:
        """剩余天数窗口内的链"""
        return OptionFilter().days(self.min_days, self.max_days)

    def on_init(self) -> None:
        """登记关注范围并预热"""
        portfolio = self.get_portfolio(self.portfolio_name)
        if portfolio is None:
            self.write_log(f"找不到期权组合{self.portfolio_name}")
            return
        self.subscribe_options(self.portfolio_name, self.scope())

        underlyings = {f"{chain.underlying_symbol}.{chain.exchange.value}" for chain in portfolio.chains.values()}
        for vt_symbol in sorted(underlyings):
            self.load_bars(vt_symbol, self.preload_days)
        self.write_log("策略初始化完成")

    def on_bars(self, bars: dict[str, BarData]) -> None:
        return

    def on_snapshot(self, snapshot: Snapshot) -> None:
        """选定合约并调仓"""
        if not self.trading:
            return

        portfolio = self.get_portfolio(self.portfolio_name)
        if portfolio is None:
            return
        if self.target_symbol not in portfolio.options:
            if self.target_symbol:
                self.write_log(f"目标合约{self.target_symbol}今日不在组合中，重新选择")
                self.clear_targets()
                self.target_symbol = ""
            chains = self.scope().chains(portfolio)
            option = chains[0].select_by_delta(1, self.target_delta) if chains else None
            if option is None:
                return
            self.target_symbol = option.vt_symbol
            self.set_target(self.target_symbol, self.target_volume)
            self.put_event()

        self.execute_trading({self.target_symbol: portfolio.options[self.target_symbol].mid}, self.percent_add)

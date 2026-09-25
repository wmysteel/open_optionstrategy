"""示例策略：卖跨式加 delta 对冲"""
import numpy as np
from vnpy.trader.object import BarData

from open_optionstrategy.object import ChainData, OptionFilter, PortfolioData, Snapshot
from open_optionstrategy.template import Parameter, StrategyTemplate, Variable

COMBO = "straddle"


class ShortStraddleStrategy(StrategyTemplate):
    """示例：卖平值跨式，用标的期货对冲 delta；临近到期或当日亏损超限时全部平仓"""

    author = "open_optionstrategy"

    portfolio_name: str = Parameter("", "期权组合")
    min_days: int = Parameter(20, "最少剩余天数")
    max_days: int = Parameter(60, "最多剩余天数")
    volume: int = Parameter(1, "份数")
    hedge_band: float = Parameter(0.5, "对冲阈值（期货手数）")
    stop_loss: float = Parameter(0.0, "当日止损金额")
    exit_days: int = Parameter(5, "剩余几天平仓")
    percent_add: float = Parameter(0.0, "超价比例")

    legs: dict = Variable({}, "持仓腿")
    future: str = Variable("", "对冲期货")
    stopped_day: str = Variable("", "止损日")

    def scope(self) -> OptionFilter:
        """剩余天数窗口内的链"""
        return OptionFilter().days(self.min_days, self.max_days)

    def on_init(self) -> None:
        """登记关注范围"""
        self.subscribe_options(self.portfolio_name, self.scope())

    def on_bars(self, bars: dict[str, BarData]) -> None:
        return

    def on_snapshot(self, snapshot: Snapshot) -> None:
        """开仓，两腿建完后对冲，到期前或止损时平仓"""
        if not self.trading:
            return
        portfolio = self.get_portfolio(self.portfolio_name)
        if portfolio is None:
            return
        if not self.legs:
            if self.future and self.get_pos(self.future):   # 上一轮的对冲期货还没平完，平完再开新仓
                self.execute_trading({self.future: self.get_price(self.future)}, self.percent_add)
                return
            if not self.open(portfolio):
                return
        options = [portfolio.options[leg] for leg in self.legs if leg in portfolio.options]
        if len(options) < len(self.legs):
            self.write_log("持仓腿今日不在组合中，重新选腿")
            self.clear_targets()
            self.set_target(self.future, 0)
            self.legs = {}
            self.put_event()
            return
        chain = options[0].chain
        if chain.days_to_expiry() <= self.exit_days or self.stop_hit():
            self.set_combo_target(COMBO, self.legs, 0)
            self.set_target(self.future, 0)
            if all(o.pos == 0 for o in options) and self.get_pos(self.future) == 0:
                self.legs, self.future = {}, ""
                self.put_event()
                return
        elif all(o.pos == self.legs[o.vt_symbol] * self.volume for o in options):
            self.hedge()
        prices = {o.vt_symbol: o.mid for o in options}
        prices[self.future] = self.get_price(self.future)
        self.execute_trading(prices, self.percent_add)

    def open(self, portfolio: PortfolioData) -> bool:
        """在最近的链上卖平值认购与认沽；当天止损过就不开"""
        if self.stopped_day == str(self.get_today()):
            return False
        chains = self.scope().chains(portfolio)
        if not chains:
            return False
        chain: ChainData = chains[0]
        call, put = chain.get_option_by_level(1, 0), chain.get_option_by_level(-1, 0)
        if call is None or put is None:
            return False
        self.legs = {call.vt_symbol: -1, put.vt_symbol: -1}
        self.future = f"{chain.underlying_symbol}.{chain.exchange.value}"
        self.set_combo_target(COMBO, self.legs, self.volume)
        self.put_event()
        return True

    def stop_hit(self) -> bool:
        """当日亏损超过止损金额：记下止损日，当天剩下的时间只平不开"""
        today = str(self.get_today())
        if self.stopped_day != today and self.stop_loss and self.get_pnl_today() < -self.stop_loss:
            self.stopped_day = today
            self.write_log(f"当日亏损 {-self.get_pnl_today():.0f} 超过 {self.stop_loss:.0f}，全部平仓，当天不再开仓")
            self.put_event()
        return self.stopped_day == today

    def hedge(self) -> None:
        """组合净 delta 超过阈值时，用期货调回到接近 0"""
        delta = self.get_greeks()["delta"]
        size = self.get_size(self.future)
        if size is None or np.isnan(delta):
            return
        lots = delta / size
        if abs(lots) > self.hedge_band:
            self.set_target(self.future, self.get_pos(self.future) - round(lots))

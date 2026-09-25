# open_optionstrategy

基于 VeighNa（vnpy）的开源期权策略应用：在 vnpy 里开发、回测和实盘运行全市场期权策略。

> 这是独立开发的第三方模块，不是 VeighNa 官方出品。

## 它能做什么

- **全市场实时 IV 与希腊值**：每 0.5 秒一个节拍，对全部期权一次算完隐含波动率（按中间价、买价、卖价三种）和 delta、gamma、vega、theta。策略直接读结果，不用自己算。
- **期权链与组合**：行情按"期权组合 → 链（同一标的、同一到期日）→ 期权"组织。取平值、按档位取、按 delta 选合约都有现成方法；`OptionFilter` 可以按剩余天数、系列、行权价范围、认购认沽筛选。
- **按目标仓位调仓**：策略只需要设目标手数，单个合约或多腿组合都可以。引擎负责先平后开、挂单不够时补差额、超价后截在涨跌停之内、控制撤单重挂的节奏。组合的各条腿按成交难度同步推进；一条腿被柜台拒绝时，撤掉其他腿的挂单。
- **重启恢复**：持仓、目标仓位、在途委托及其已入账的成交、当日盈亏的计算依据，都随数据文件保存。同一交易日重启后，停机期间和登录时柜台重推的成交会补记入账，当日盈亏接着算。
- **回测**：支持 BAR（分钟线）和 TICK（逐笔）两种模式，策略代码与实盘相同。
  - 分钟和 tick 数据按交易日缓存；BAR 模式预先算好的 IV 与希腊值随分钟缓存复用。
  - 按逐日盯市计算盈亏。统计指标在 vnpy 原有指标之外，增加了卡玛比率、索提诺比率、VaR/CVaR、欧米茄比率、按回合计的胜率与盈亏比；统计口径有两处与 vnpy 不同，见"回测"一节。
  - 支持穷举和遗传算法两种参数优化。
- **界面**：挂在 vnpy 主界面，菜单名"期权策略"。可以增删改策略、初始化、启停，查看参数、变量和日志。

**本模块不含任何交易所规则。** 交易日历、期权链归属、定价模型、标的价、保证金、手续费、到期结算，全部由你提供的"规则实现"给出，见下文。

## 环境要求

- Python 3.10 及以上
- vnpy 4.4.0 及以上（用到了 4.4 才有的 `MainEngine.send_notification`）
- numpy、scipy、pandas、pyarrow、plotly（安装时自动安装）

## 安装

```bash
git clone https://github.com/wmysteel/open_optionstrategy.git
cd open_optionstrategy
pip install .
```

## 在 vnpy 中加载

```python
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow, create_qapp
from vnpy_ctp import CtpGateway

from open_optionstrategy import OptionStrategyApp

qapp = create_qapp()
event_engine = EventEngine()
main_engine = MainEngine(event_engine)
main_engine.add_gateway(CtpGateway)
main_engine.add_app(OptionStrategyApp)

main_window = MainWindow(main_engine, event_engine)
main_window.showMaximized()
qapp.exec()
```

- **交易接口**：示例里的 `vnpy_ctp` 不随本模块安装，要另外 `pip install vnpy_ctp`。
- **策略文件**：放在运行目录下的 `strategies` 文件夹里。包里自带七个示例策略，清单见下文"编写策略"一节。运行目录里与示例同名的策略类会替换掉示例，日志里会记一条。
- **初始化时机**：初始化策略前，要等交易接口的合约查询完成。用 vnpy_ctp 一类接口时，引擎会自动检查：合约没查完就拒绝初始化，并提示等日志出现"合约信息查询成功"。

## 配置

写在运行目录的 `.vntrader/vt_setting.json` 里：

```json
{
    "optionstrategy.profiles": "my_rules:MyProfile",
    "optionstrategy.execution": {"reprice_ticks": 2, "min_reprice_seconds": 3},
    "optionstrategy.risk_free_rate": 0.02
}
```

| 键 | 含义 | 默认 |
|---|---|---|
| `optionstrategy.profiles` | 规则实现，逗号分隔的"模块:对象"，对象可以是类或实例 | 空 |
| `optionstrategy.execution` | 调仓的执行参数，见下表；写了不认识的键会直接报错，免得拼错了悄悄不生效 | 全部取默认 |
| `optionstrategy.risk_free_rate` | 定价用的无风险利率（年化，小数） | 0.02 |

`optionstrategy.execution` 的各项如下：

| 键 | 含义 | 默认 |
|---|---|---|
| `reprice_ticks` | 调仓时挂单价与新价相差不超过这么多个最小价位就不撤 | 2 |
| `min_reprice_seconds` | 撤单重挂后隔多少秒才再发；柜台拒单后同样起算，当日每再被拒一次，间隔加倍 | 3 |
| `cancel_retry_seconds` | 撤单发出后委托仍然活动（柜台拒绝撤单），隔多少秒可以再撤 | 5 |

策略的配置和运行数据，引擎自动读写 `.vntrader/option_strategy_setting.json` 和 `.vntrader/option_strategy_data.json`，不需要手工改。

## 规则实现（必须自己提供）

规则实现告诉引擎各个交易所的规则。可以用两种方式登记：

- 在 `vt_setting.json` 的 `optionstrategy.profiles` 里写"模块:对象"；
- 在你自己的包里注册 entry point，组名为 `open_optionstrategy.profiles`：

  ```toml
  [project.entry-points."open_optionstrategy.profiles"]
  my_rules = "my_rules:MyProfile"
  ```

有多个规则实现时，按顺序由第一个 `match` 返回真的规则实现负责该合约。没有任何规则实现认领的合约，不进当日合约表。

一个规则实现要提供下面这些成员：

| 成员 | 作用 | 调用时机 | 单位与约定 |
|---|---|---|---|
| `name`、`version` | 名字与规则版本 | — | 改了影响定价或希腊值的规则就改 `version`，回测缓存里预先算好的希腊值会随之作废 |
| `match(contract)` | 是否认领这个合约 | 每个交易日建合约表时 | 返回真即由本规则实现负责 |
| `calendar.trading_day_of(dt)` | 某一时刻属于哪个交易日 | 建日、判断换日、回测归日与判断交易日 | 夜盘归下一交易日；对任意时刻（包括周末、节假日）都要给出结果，非交易日的时刻归到相邻交易日，结果随时间只增不减。回测把"某日 12:00 属于当天"的日子当作交易日 |
| `calendar.trading_days_between(start, end)` | 两个交易日之间隔几个交易日 | 按交易日计剩余天数时 | 整数 |
| `calendar.expiry_timestamps(contracts)` | 各合约的到期时刻 | 建日时 | 数组，Unix 时间戳（秒），不到期的合约为 NaN；到期时刻已过的合约不进当日合约表 |
| `contract_info.chain_key(contract)` | 期权属于哪个组合、哪条链 | 建日时 | 返回（组合名, 标的代码, 到期日）；返回 `None` 表示用合约自带的 `option_portfolio`、`option_underlying`、`option_expiry` |
| `contract_info.series_of(contract)` | 系列标签 | 建日时 | 字符串，如"标准"，供 `OptionFilter.series` 筛选 |
| `contract_info.contract_attributes(contract)` | 期权属性 | 建日时 | `OptionAttributes(行权方式, 最后行权日, 单笔最大报单量, 最小开仓量)`；最后行权日晚于合约的 `option_expiry` 时，回测里合约留到最后行权日，持仓在那天结算 |
| `contract_info.pricing_model(contract)` | 这个期权用哪个定价模型 | 建日时 | 可以直接用本包 `pricing` 模块的 `Black76`、`BlackScholes`、`BinomialTree`；同类型、同参数的模型会自动并成一组，一次算完。期货期权用 `Black76`（欧式）或 `BinomialTree(futures=True)`（美式）；二叉树的希腊值有离散误差（默认 100 步，vega 一般差 1% 到 2%），要更准就加大 `steps` |
| `underlying.build_plan(contracts, slots)` | 建当日的标的价计划 | 建日时一次 | 返回值原样传给 `underlying_prices` |
| `underlying.underlying_prices(plan, snapshot)` | 各槽位的标的价 | 每个节拍 | 与槽位等长的数组，不认领的槽位为 NaN |
| `margin_model.margins(price, underlying, strike, cp, size, is_option)` | 每手多头、每手空头保证金 | 每个节拍 | 两个与槽位等长的数组（元/手），不认识的槽位为 NaN；用于 `get_margin`（本策略保证金占用）和回测里的可用资金 |
| `cost_model.commission(trade)` | 一笔成交的手续费 | 回测里逐笔成交时 | 元；回测给了 `rate` 参数时不调用（`rate` 是字典时，字典里没有的合约仍调用） |
| `settlement.settle_at_expiry(vt_symbol, volume, settle_price)` | 到期结算 | 回测的到期日日终 | `volume` 是带方向的持仓手数，`settle_price` 是标的价；返回 `Settlement(合约, 了结的手数（与持仓同方向）, 新生成的持仓 {合约: 带方向手数}, 现金损益（元）)` |
| `on_start(main_engine)` | 实盘开始时的准备 | 实盘建好当日状态后一次 | 传入 vnpy 主引擎；回测不调用 |
| `close()` | 释放资源 | 引擎关闭时 | — |

**自己写定价模型时的口径**：
- **输入**：`f`、`k`、`t`、`v`、`cp` 是数组，`r` 是标量。`t` 是年化剩余时间，按自然日、一年 365 天；`r` 是年化利率；`v` 是年化波动率（小数）；`cp` 为 1 表示认购、-1 表示认沽。
- **`greeks` 返回值**：返回"单位希腊值"，也就是不乘合约乘数；其中 vega 是波动率变动 1（即 100 个百分点）时的价格变动，theta 是每年的价格变动。引擎会统一换算成下文的对外口径。
- **`implied_vol`**：引擎按 `implied_vol(p, f, k, t, r, cp, v0=上一拍的 IV)` 调用。`p` 可能是一维数组，也可能是买价、卖价两行（形状为 (2, n)），要按 numpy 的规则与一维的 `f`、`k`、`t`、`cp` 广播，返回与 `p` 同形状的结果；解不出时为 NaN。

下面是一个商品期货期权的骨架，只示意结构，具体规则请按你的交易所和期货公司填写：

```python
import numpy as np
from vnpy.trader.constant import Exchange, Product
from vnpy.trader.object import ContractData

from open_optionstrategy.base import ExerciseStyle
from open_optionstrategy.object import OptionAttributes, Settlement, Snapshot
from open_optionstrategy.pricing import Black76


class MyCalendar:
    def trading_day_of(self, dt): ...                    # 夜盘归下一交易日
    def trading_days_between(self, start, end): ...
    def expiry_timestamps(self, contracts): ...          # Unix 秒，不到期的合约为 NaN


class MyContractInfo:
    model = Black76()

    def chain_key(self, contract):
        return None                                      # 用合约自带的组合、标的、到期日

    def series_of(self, contract):
        return "标准"

    def contract_attributes(self, contract):
        return OptionAttributes(ExerciseStyle.AMERICAN, contract.option_expiry.date(), 1000, 1)

    def pricing_model(self, contract):
        return self.model


class MyUnderlying:
    def build_plan(self, contracts, slots):
        """期权槽位 → 标的期货槽位"""
        plan = np.full(len(slots), -1)
        for c in contracts:
            if c.product == Product.OPTION:
                plan[slots[c.vt_symbol]] = slots.get(f"{c.option_underlying}.{c.exchange.value}", -1)
        return plan

    def underlying_prices(self, plan, snapshot: Snapshot):
        cols = snapshot.columns
        two_sided = np.isfinite(cols["bid1"]) & np.isfinite(cols["ask1"])
        mid = np.where(two_sided, (cols["bid1"] + cols["ask1"]) / 2, cols["last"])
        prices = np.full(snapshot.slot_count, np.nan)
        mapped = plan >= 0
        prices[mapped] = mid[plan[mapped]]
        return prices


class MyProfile:
    name = "my_rules"
    version = "1"

    def __init__(self):
        self.calendar = MyCalendar()
        self.contract_info = MyContractInfo()
        self.underlying = MyUnderlying()
        self.margin_model = ...                          # 实现 margins(...)
        self.cost_model = ...                            # 实现 commission(trade)
        self.settlement = ...                            # 实现 settle_at_expiry(...)

    def match(self, contract: ContractData) -> bool:
        return contract.exchange == Exchange.DCE

    def on_start(self, main_engine) -> None:
        return None

    def close(self) -> None:
        return None
```

## 编写策略

策略继承 `StrategyTemplate`，用 `Parameter`、`Variable` 声明参数和变量（界面显示中文名），在回调里读视图、设目标仓位：

| 回调 | 时机 |
|---|---|
| `on_init` | 初始化：登记关注范围、用 `load_bars` 预热 |
| `on_start` / `on_stop` | 启动、停止 |
| `on_snapshot(snapshot)` | 每个有新行情的节拍（没有新行情的节拍不调用）：IV 与希腊值已经算好，经 `get_portfolio` 取视图读取 |
| `on_bars(bars)` | 关注范围内合约的分钟 K 线 |
| `on_tick(tick)` | `subscribe_data` 登记过的合约的行情 |
| `on_expiry(options)` | 启动后的当日首个节拍，本策略持有的、今天是最后行权日的期权；平仓、换月还是交给柜台行权，由策略自己决定 |
| `update_order` / `update_trade` | 委托、成交回报（持仓由引擎记账，一般不用重载） |

常用接口：

- **读行情与希腊值**：
  - `get_portfolio(组合名)` 返回当日的组合视图，下有 `chains`、`options` 和 `greeks()`。
  - 链上有 `underlying_price`、`atm_strike`、`get_option_by_level(cp, 档位)`、`select_by_delta(cp, 目标 delta)`。
  - 期权上有 `bid`、`ask`、`mid`、`volume`（当日累计成交量）、`open_interest`（持仓量）、`iv`、`delta`、`gamma`、`vega`、`theta`、`pos`；当日还没来过行情的为 NaN。例如在标的价到标的价 +10% 之间挑持仓量最大的认购（`o.open_interest > 0` 同时去掉了 NaN）：

    ```python
    candidates = [o for o in StrikeRange.atm_percent(0, 0.10).options(chain, cp=1) if o.open_interest > 0]
    option = max(candidates, key=lambda o: o.open_interest, default=None)
    ```
  - `get_price(合约)` 查任意合约（包括期货）的最新盯市价：有合理的双边报价取中间价，否则取最近成交价或最新价；某拍没有价格时沿用当日最近一次的有效价，当日还没有过为 NaN。
  - `get_theo_price(合约, 波动率, 标的价=None, days=0)` 用这个期权自己的定价模型、按给定波动率算理论价（不乘合约乘数）；不给标的价用当前的，`days` 为假设过了几个自然日，过了到期取内在价值。
- **选合约**：例如 `OptionFilter().days(10, 40).strikes(StrikeRange.atm_relative(2, 2)).calls_only().options(portfolio)`。行权价范围还可以用 `StrikeRange.fixed`、`atm_percent`、`delta`。
- **关注范围**：
  - `subscribe_options(组合名, 条件)` 登记后，实盘向交易接口订阅这些合约的行情，并推送它们的 K 线。
  - `subscribe_data(合约)` 登记的合约推送 `on_tick`。
  - 有持仓或挂单的合约会自动留在关注范围里。
- **交易**：
  - `set_target(合约, 手数)` 或 `set_combo_target(名字, {合约: 比例}, 份数)` 设目标，然后 `execute_trading({合约: 价格}, 超价比例)` 调仓。
  - 也可以直接用 `buy`、`sell`、`short`、`cover`。
  - `get_combo_pos(组合名)` 查本策略这个组合目标已建成几份：各腿持仓除以比例，取离 0 最近的那条腿；各腿方向不一致时为 0。
- **持仓与希腊值**：`get_pos(合约)`、`get_greeks(组合名)`。不给组合名时，汇总策略的全部持仓，包括标的期货。
  - `get_combo_greeks({合约: 比例})` 按一组腿和带方向的比例合计希腊值，下单前预估、或单看策略里某一个组合都能用。例如比例价差买 1 张 C3000、卖 2 张 C3100 的净 delta：`get_combo_greeks({"m2601-C-3000.DCE": 1, "m2601-C-3100.DCE": -2})["delta"]`；想按实际持仓算，就把各腿的持仓手数当比例传。有一条腿不在当日主档或希腊值无效时，该项为 NaN。
  - `get_combo_premium({合约: 比例}, cross=False)` 按当前行情算一组腿的净权利金（元，已乘合约乘数），正数为净支付、负数为净收入；默认按中间价，`cross=True` 时买的腿取卖价、卖的腿取买价。有一条腿不在当日主档或没有报价时为 NaN。
  - `get_combo_margin({合约: 比例})` 预估一组腿占用的保证金（元）：买的腿按每手多头保证金、卖的腿按每手空头保证金，乘手数相加，用来在下单前定手数。交易所的组合保证金优惠不在里面，结果偏保守；有一条腿不在当日主档或当日还没算出过保证金时为 NaN。
  - 这三个组合接口只读当时的行情、希腊值与保证金，不看哪个策略的持仓；合约不分交易所，都能放进一组。不同标的的 delta 相加没有意义，算净 delta 时一组腿应是同一个标的。
- **持仓成本与浮动盈亏**（本策略自己的）：
  - `get_cost(合约)` 本策略这个合约持仓的开仓均价：加仓按成交量加权，减仓不变，一笔成交让持仓翻了方向时取这笔的价格，空仓为 NaN。均价随数据文件保存，重启后接着用；回测里行权得到的期货按标的价记。升级前已有的持仓没有成本记录，为 NaN。
  - `get_open_pnl([合约...])` 这些合约持仓的浮动盈亏（元）= 持仓 ×（最新盯市价 − 开仓均价）× 乘数，不给合约就算全部持仓；有持仓却没有成本或价格时为 NaN。例如卖出的组合收到的权利金赚到一半就平仓：

    ```python
    legs = list(self.legs)                                                                  # 策略记下的组合各腿
    received = -sum(self.get_pos(s) * self.get_cost(s) * self.get_size(s) for s in legs)   # 开仓净收到的权利金（元）
    if received > 0 and self.get_open_pnl(legs) >= 0.5 * received:
        self.set_combo_target("spread", self.legs, 0)
    ```
- **风控用的信息**：`get_pnl_today()` 本策略当日盈亏（元），`get_margin()` 本策略持仓占用的保证金（元；某合约某拍算不出时沿用它当日上一次的值），`get_available()` 账户可用资金（元，拿不到时为 None）。

示例策略 `DeltaTargetStrategy` 的核心：

```python
def on_snapshot(self, snapshot) -> None:
    if not self.trading:
        return
    portfolio = self.get_portfolio(self.portfolio_name)
    if portfolio is None:
        return
    if self.target_symbol not in portfolio.options:
        chains = self.scope().chains(portfolio)                 # 剩余天数窗口内的链，按到期日从近到远
        option = chains[0].select_by_delta(1, self.target_delta) if chains else None
        if option is None:
            return
        self.target_symbol = option.vt_symbol
        self.set_target(self.target_symbol, self.target_volume)
    self.execute_trading({self.target_symbol: portfolio.options[self.target_symbol].mid}, self.percent_add)
```

包里自带的示例策略（`open_optionstrategy/strategies/`，引擎启动时自动加载），每个演示一项主要功能：

| 类名 | 策略 | 演示的功能 |
|---|---|---|
| `DeltaTargetStrategy` | 按 delta 选一个认购，按目标手数调仓 | 单合约目标、`select_by_delta`、用 `load_bars` 预热 |
| `ShortStraddleStrategy` | 卖平值认购与认沽，用标的期货对冲；剩 5 天或当日亏损超过止损金额时全部平仓 | 两腿组合目标；`get_greeks()` 汇总含期货的净 delta；期货对冲；用 `get_pnl_today()` 做策略自己的止损 |
| `BullCallSpreadStrategy` | 买平值认购、卖高 2 档认购，到期日平仓 | 按档位选合约；`on_expiry` 到期回调 |
| `CoveredCallStrategy` | 持有标的期货 1 手、卖虚值 2 档认购；剩 5 天时换到下一条链，标的期货不同就一起换 | 期货与期权混合持仓；跨链换月；用策略变量记平仓中的合约 |
| `IronCondorStrategy` | 卖 delta 0.25 的认购与认沽，买 delta 0.10 的保护；剩 5 天时平仓，没有买价的保护腿留到到期作废 | 四腿组合目标；用 `StrikeRange.delta` 圈定关注范围 |
| `RatioSpreadStrategy` | 买 1 张平值认购、卖 2 张高 2 档认购；剩 5 天时平仓 | 比例不为 1 的组合目标 |
| `ButterflyStrategy` | 买低 2 档与高 2 档认购、卖 2 张平值认购；剩 5 天时平仓，没有买价的翼留到到期作废 | 三腿组合目标、翼宽参数 |

除 `DeltaTargetStrategy` 外，这些示例默认都在剩余 20 到 60 天（两端不含）的最近一条链上开 1 份，每个节拍按中间价调仓；平仓完成后，在窗口内的链上重新开，一直滚动下去。持仓腿到期后不在当日组合里时，清空目标、重新选腿。

## 回测

```python
from datetime import date

from open_optionstrategy.backtesting import BacktestingEngine
from open_optionstrategy.base import BacktestingMode
from open_optionstrategy.strategies.delta_target_strategy import DeltaTargetStrategy

engine = BacktestingEngine()
engine.set_parameters(
    contracts=contracts,                 # 回测期间要用到的全部合约（ContractData 列表）
    start=date(2026, 3, 2),
    end=date(2026, 3, 31),
    capital=1_000_000,
    slippage=0.5,
    mode=BacktestingMode.BAR,
    cache=True,
)
engine.add_strategy(DeltaTargetStrategy, {"portfolio_name": "m_o", "target_volume": -1})
engine.load_data()
engine.run_backtesting()
df = engine.calculate_result()
engine.calculate_statistics()
engine.show_chart().show()
```

- **合约列表 `contracts`**：由你自己准备，比如实盘时把 `main_engine.get_all_contracts()` 存下来。引擎按合约的上市日（`option_listed`）和到期日（`option_expiry`，最后行权日更晚时取最后行权日）逐日筛出当天的合约；这两个字段为空的合约每天都算在内。
- **历史数据**：从 vnpy 数据库取。BAR 模式用分钟线，TICK 模式用 tick。规则实现同样从 `vt_setting.json` 加载。
- **缓存**：按交易日存着当日合约主档；合约列表改了（乘数、增减合约、换规则实现），该日改从数据库取并回写。
- **手续费与滑点**：
  - 手续费默认走规则实现的 `cost_model`；给了 `rate` 时，按成交额乘以 `rate` 计算。
  - 滑点按成交手数 × 合约乘数 × `slippage` 计算。
  - `rate`、`slippage`、成交价模型等参数，可以对全部合约给一个数，也可以给字典，键是合约的 vt_symbol（如 `m2601-C-3000.DCE`）或期权组合名；写了不认识的键会直接报错。`rate` 是字典时，字典里没有的合约仍按 `cost_model` 算手续费。
- **成交价模型 `fill_model`**：
  - `natural`：按对手价成交；
  - `mid`：按中间价成交；
  - `spread_ratio`：在己方价与对手价之间按 `fill_ratio` 取，0 为己方价，1 为对手价。
  
  `fill_volume_ratio` 限制成交不超过市场成交量的这个比例：委托可以成交期间，每段（BAR 模式每分钟，TICK 模式每笔 tick）按"本段成交量 × 比例"累计可成交量，攒够整数手才成交，同一合约的几笔委托共用；合约没有活动委托时清零。流动性差的合约可以设 0.2 左右。
- **BAR 模式的盘口是估算的**：vnpy 数据库的分钟线没有盘口，只能在成交量大于 0 的那一分钟按收盘价估一个盘口：价差为 `est_spread_ticks` 个最小价位（记为 n），买价在收盘价下方 ⌊n/2⌋ 个价位，卖价比买价高 n 个价位，都落在价位上。这个盘口只在那一分钟有效；之后没成交的分钟没有盘口，也不能成交。vnpy 按 tick 最新价合成的成交量为 0 的分钟线不估盘口。统计里的"估算盘口成交占比"告诉你有多少成交落在估算盘口上。
- **统计口径与 vnpy 的两处不同**：
  - 首日收益按起始资金算，回撤的高点从起始资金起算；vnpy 丢掉首日收益、从首日收盘后的资金起算，首日大亏时显示没有回撤。
  - `risk_free` 是年化小数，按 `risk_free / annual_days` 折成日收益；vnpy 除以 `annual_days` 的平方根。
  
  另外，比率的分母为 0 时（例如没有亏损日、没有回撤、没有亏损的回合），有收益记为无穷大，否则记为 0；vnpy 一律记 0，拿来做优化目标时排序会颠倒。
- **日志**：回测里引擎自己的日志（规则实现加载、拒单、到期结算等）会打印出来，也记在 `engine.logs` 里；策略的 `write_log` 只记在 `engine.logs`。
- **参数优化**：`run_bf_optimization` 穷举，`run_ga_optimization` 用遗传算法。每组以 `add_strategy` 给的策略参数为底，只换被优化的参数。各组在子进程里跑，规则实现要能被 pickle（不要持有数据库连接之类的对象）。

## 希腊值口径

| 名称 | 口径 |
|---|---|
| delta | 每张合约，已乘合约乘数，标的价格变动 1 元时的盈亏 |
| gamma | 每张合约，已乘合约乘数，标的价格变动 1 元时 delta 的变化 |
| vega | 每张合约，已乘合约乘数，波动率变动 1 个百分点时的盈亏 |
| theta | 每张合约，已乘合约乘数，每过一个自然日的盈亏 |
| iv | 小数，界面显示为百分数 |

另有不乘合约乘数的 `delta_unit`，按 delta 选合约时用。持仓的希腊值汇总时，只要有一个持仓合约的希腊值无效（比如 IV 解不出），该项合计就是 NaN，不会按 0 计。

## 注意事项

- **壳不做风控**：报单频率、报撤单次数、持仓上限、资金、亏损止损、行情异常时停开仓，每个交易者要求不同，由策略自己用上面三个查询接口实现。壳只做这几项检查：合约存在、价格与数量有效、平仓不超过本策略可平持仓、交易所最小开仓量；超过单笔最大报单量的委托自动拆成几笔。
- **初始化期间其他策略暂停调仓**：策略初始化（包括 `load_bars` 预热拉历史数据）在引擎唯一的工作线程里进行，这期间其他策略的调仓、撤单与回报处理都在排队。盘中初始化要预热的策略时请留意。
- **超价比例是小数**：`execute_trading` 的超价比例，0.01 表示超价 1%。
- **`clear_targets` 只清目标，不平仓**：要平仓，把目标设为 0。
- **一个合约只能有一个目标**：同一个合约只能属于一个组合目标，也不能同时有单合约目标，否则 `set_target`、`set_combo_target` 抛出 ValueError；在策略回调里抛出的异常会让策略停下。
- **组合视图只在当日有效**：换日后，旧视图的槽位对应的是别的合约，再读会报错。请在回调里用 `get_portfolio` 取当日视图，不要跨日存着。
- **实盘到期不由引擎结算**：实盘里到期行权由柜台处理，引擎在到期当天回调 `on_expiry`。到期后，当日合约表里已经没有的持仓会留在数据文件里，每次启动时提醒人工核对。
- **示例策略不构成交易建议**：示例策略只用来演示用法，默认参数（如 `DeltaTargetStrategy` 的目标手数 -1，即卖出 1 张认购）使用前请按需修改。

## 风险声明

本项目按"现状"提供，不构成任何投资建议。期权交易风险很高，实盘前请在模拟环境充分测试。使用本项目造成的任何损失，由使用者自行承担。

## 许可

MIT 许可，见 [LICENSE](LICENSE)。部分代码参照 VeighNa 的 vnpy_portfoliostrategy、vnpy_ctastrategy 编写，其 MIT 版权声明一并附在 LICENSE 中。

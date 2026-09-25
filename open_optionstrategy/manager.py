"""合约管理器"""
from datetime import date

import numpy as np
from vnpy.trader.constant import OptionType, Product
from vnpy.trader.object import ContractData

from .base import GREEK_COLUMNS, GREEK_NAMES, Quality
from .object import (
    ChainData,
    MarketProfile,
    MarketState,
    OptionAttributes,
    PortfolioData,
    PricingModel,
    Snapshot,
    pick_nearest,
    route_profiles,
)
from .pricing import iv_price_source

STRIKE_KEY = 1e8
SECONDS_PER_YEAR = 365 * 86400


def _readonly(array: np.ndarray) -> np.ndarray:
    view = array.view()
    view.flags.writeable = False
    return view


class ContractManager:
    """当日合约主档"""

    def __init__(self, contracts: list[ContractData], profiles: list[MarketProfile]) -> None:
        slots: list[str] = [c.vt_symbol for c in contracts]
        self.slots = slots
        self.slot_index: dict[str, int] = {vt_symbol: i for i, vt_symbol in enumerate(slots)}
        n = len(slots)

        self.size = np.ones(n)
        self.pricetick = np.full(n, np.nan)
        self.strike = np.full(n, np.nan)
        self.cp = np.zeros(n)
        self.is_option = np.zeros(n, dtype=bool)
        self.chain_of_slot = np.full(n, -1, dtype=np.int64)
        self.contracts: dict[str, ContractData] = {}
        self.attributes: dict[str, OptionAttributes] = {}
        self.profile_of = route_profiles(contracts, profiles)
        self.chain_keys: list[tuple[str, str, date]] = []
        self.chain_series: list[str] = []
        chain_ids: dict[tuple[str, str, date], int] = {}
        for contract in contracts:
            self._register(contract, chain_ids)

        self.chain_first_slot, self._flat_strike, self._flat_key, self._offsets = self._atm_index()
        self.plans, self.profile_slots, self.expiry_ts, self.model_groups = self._pricing_plans(profiles)
        self.model_of: dict[int, PricingModel] = {int(slot): model for model, idx in self.model_groups for slot in idx}

        self.iv = np.full(n, np.nan)
        self.last_trade_price = np.full(n, np.nan)
        self.last_trade_ts = np.full(n, -np.inf)
        self.last_volume = np.zeros(n)

    def _register(self, contract: ContractData, chain_ids: dict[tuple[str, str, date], int]) -> None:
        """登记一个合约"""
        slot = self.slot_index[contract.vt_symbol]
        self.contracts[contract.vt_symbol] = contract
        self.size[slot] = contract.size
        self.pricetick[slot] = contract.pricetick
        if contract.product != Product.OPTION:
            return
        self.is_option[slot] = True
        profile = self.profile_of.get(contract.vt_symbol)
        if profile is None:
            return
        key = profile.contract_info.chain_key(contract) or (
            contract.option_portfolio, contract.option_underlying, contract.option_expiry.date()
        )
        if key not in chain_ids:
            chain_ids[key] = len(self.chain_keys)
            self.chain_keys.append(key)
            self.chain_series.append(profile.contract_info.series_of(contract))
        self.attributes[contract.vt_symbol] = profile.contract_info.contract_attributes(contract)
        self.strike[slot] = contract.option_strike
        self.cp[slot] = 1.0 if contract.option_type == OptionType.CALL else -1.0
        self.chain_of_slot[slot] = chain_ids[key]

    def _atm_index(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """平值查找表"""
        chain_count = len(self.chain_keys)
        chain_first_slot = np.zeros(chain_count, dtype=np.int64)
        strikes_per_chain: list[np.ndarray] = []
        for chain_id in range(chain_count):
            members = np.flatnonzero(self.chain_of_slot == chain_id)
            chain_first_slot[chain_id] = members[0]
            strikes_per_chain.append(np.unique(self.strike[members]))
        if not chain_count:
            return chain_first_slot, np.empty(0), np.empty(0), np.zeros(1, dtype=np.int64)
        flat_strike = np.concatenate(strikes_per_chain)
        flat_key = np.concatenate([i * STRIKE_KEY + ks for i, ks in enumerate(strikes_per_chain)])
        offsets = np.cumsum([0] + [ks.size for ks in strikes_per_chain])
        return chain_first_slot, flat_strike, flat_key, offsets

    def _pricing_plans(
        self, profiles: list[MarketProfile]
    ) -> tuple[list[tuple[MarketProfile, object]], list[tuple[MarketProfile, np.ndarray]], np.ndarray,
               list[tuple[PricingModel, np.ndarray]]]:
        """当日定价计划"""
        plans: list[tuple[MarketProfile, object]] = []
        profile_slots: list[tuple[MarketProfile, np.ndarray]] = []
        expiry_ts = np.full(len(self.slots), np.nan)
        groups: dict[tuple, tuple[PricingModel, list[int]]] = {}
        for profile in profiles:
            mine = [c for c in self.contracts.values() if self.profile_of.get(c.vt_symbol) is profile]
            if not mine:
                continue
            plans.append((profile, profile.underlying.build_plan(mine, self.slot_index)))
            idx = np.array([self.slot_index[c.vt_symbol] for c in mine])
            profile_slots.append((profile, idx))
            expiry_ts[idx] = profile.calendar.expiry_timestamps(mine)
            for contract in mine:
                if contract.product == Product.OPTION:
                    model = profile.contract_info.pricing_model(contract)
                    key = (type(model), repr(getattr(model, "__dict__", None)))
                    groups.setdefault(key, (model, []))[1].append(self.slot_index[contract.vt_symbol])
        model_groups = [(model, np.array(group)) for model, group in groups.values()]
        return plans, profile_slots, expiry_ts, model_groups

    def compute(
        self, snapshot: Snapshot, now_ts: float, r: float, stale_seconds: float, max_spread: float
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        """一次算完全市场"""
        cols = snapshot.columns
        self._track_trades(cols["last"], cols["volume"], now_ts)
        underlying = self._underlying_prices(snapshot)
        tte = np.maximum(self.expiry_ts - now_ts, 0.0) / SECONDS_PER_YEAR
        price = iv_price_source(
            cols["bid1"], cols["ask1"], self.last_trade_price, self.last_trade_ts, now_ts, stale_seconds, max_spread
        )
        greeks = self._greeks(cols, underlying, tte, price, r)
        quality = self._quality(cols["bid1"], cols["ask1"], price, greeks)
        mark = np.where(np.isfinite(price), price, cols["last"])
        long_margin, short_margin = self._margins(mark, underlying)
        chain_underlying = underlying[self.chain_first_slot]
        atm_call, atm_put = self.atm_strikes(chain_underlying)

        slot_arrays = {**greeks, "underlying": underlying, "tte": tte, "iv_price": price, "quality": quality, "mark": mark,
                       "long_margin": long_margin, "short_margin": short_margin}
        chain_arrays = {"underlying": chain_underlying, "atm_call": atm_call, "atm_put": atm_put}
        return {k: _readonly(v) for k, v in slot_arrays.items()}, {k: _readonly(v) for k, v in chain_arrays.items()}

    def _track_trades(self, last: np.ndarray, volume: np.ndarray, now_ts: float) -> None:
        """记录新成交"""
        traded = np.isfinite(volume) & (volume > self.last_volume)
        self.last_trade_price = np.where(traded, last, self.last_trade_price)
        self.last_trade_ts = np.where(traded, now_ts, self.last_trade_ts)
        self.last_volume = np.where(np.isfinite(volume), volume, self.last_volume)

    def _underlying_prices(self, snapshot: Snapshot) -> np.ndarray:
        """各槽位标的价"""
        underlying = np.full(snapshot.slot_count, np.nan)
        for profile, plan in self.plans:
            part = profile.underlying.underlying_prices(plan, snapshot)
            underlying = np.where(np.isnan(underlying), part, underlying)
        return underlying

    def _greeks(
        self, cols: dict[str, np.ndarray], underlying: np.ndarray, tte: np.ndarray, price: np.ndarray, r: float
    ) -> dict[str, np.ndarray]:
        """IV 与希腊值"""
        n = len(self.slots)
        out = {name: np.full(n, np.nan) for name in GREEK_COLUMNS}
        out.update({f"{name}_unit": np.full(n, np.nan) for name in GREEK_NAMES})
        if all(name in cols for name in GREEK_COLUMNS):
            out.update({name: np.array(cols[name], dtype=float) for name in GREEK_COLUMNS})
            out.update({f"{name}_unit": out[name] / self.size for name in GREEK_NAMES})
        else:
            bid, ask = cols["bid1"], cols["ask1"]
            for model, idx in self.model_groups:
                f, k, t, cp = underlying[idx], self.strike[idx], tte[idx], self.cp[idx]
                iv = model.implied_vol(price[idx], f, k, t, r, cp, v0=self.iv[idx])
                out["iv"][idx] = iv
                out["bid_iv"][idx], out["ask_iv"][idx] = model.implied_vol(np.stack([bid[idx], ask[idx]]), f, k, t, r, cp, v0=iv)
                solved = np.flatnonzero(np.isfinite(iv))
                unit = model.greeks(f[solved], k[solved], t[solved], r, iv[solved], cp[solved])
                done = idx[solved]
                for name, scale in (("delta", 1), ("gamma", 1), ("vega", 100), ("theta", 365)):
                    out[f"{name}_unit"][done] = unit[name] / scale
                    out[name][done] = out[f"{name}_unit"][done] * self.size[done]
        self.iv = out["iv"]

        plain = ~self.is_option
        out["delta_unit"][plain], out["delta"][plain] = 1.0, self.size[plain]
        for name in ("gamma", "vega", "theta"):
            out[f"{name}_unit"][plain] = out[name][plain] = 0.0
        return out

    def _quality(self, bid: np.ndarray, ask: np.ndarray, price: np.ndarray, greeks: dict[str, np.ndarray]) -> np.ndarray:
        """质量位"""
        return (
            np.isfinite(bid) * Quality.HAS_BID
            | np.isfinite(ask) * Quality.HAS_ASK
            | (self.is_option & ~np.isfinite(price)) * Quality.STALE_PRICE
            | (self.is_option & ~np.isfinite(greeks["iv"])) * Quality.IV_INVALID
            | (self.is_option & (greeks["vega_unit"] < self.pricetick)) * Quality.LOW_VEGA
        ).astype(np.uint8)

    def _margins(self, mark: np.ndarray, underlying: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """每手保证金"""
        n = len(self.slots)
        long_margin, short_margin = np.full(n, np.nan), np.full(n, np.nan)
        for profile, idx in self.profile_slots:
            long_margin[idx], short_margin[idx] = profile.margin_model.margins(
                mark[idx], underlying[idx], self.strike[idx], self.cp[idx], self.size[idx], self.is_option[idx]
            )
        return long_margin, short_margin

    def atm_strikes(self, chain_underlying: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """全部链的平值"""
        chain_count = len(self.chain_keys)
        if chain_count == 0:
            return np.empty(0), np.empty(0)
        j = np.searchsorted(self._flat_key, np.arange(chain_count) * STRIKE_KEY + chain_underlying)
        first, last = self._offsets[:-1], self._offsets[1:] - 1
        k_lo = self._flat_strike[np.clip(j - 1, first, last)]
        k_hi = self._flat_strike[np.clip(j, first, last)]
        valid = np.isfinite(chain_underlying)
        call = np.where(valid, pick_nearest(k_lo, k_hi, chain_underlying, True), np.nan)
        put = np.where(valid, pick_nearest(k_lo, k_hi, chain_underlying, False), np.nan)
        return call, put

    def chain_symbol(self, chain_id: int) -> str:
        """链名"""
        _portfolio_name, underlying, expiry = self.chain_keys[chain_id]
        return f"{underlying}-{expiry:%Y%m%d}"

    def build_portfolios(self, state: MarketState, pos: np.ndarray) -> dict[str, PortfolioData]:
        """建组合视图"""
        portfolios: dict[str, PortfolioData] = {}
        chains: list[ChainData] = []
        for chain_id, (portfolio_name, underlying, expiry) in enumerate(self.chain_keys):
            first = self.contracts[self.slots[self.chain_first_slot[chain_id]]]
            portfolio = portfolios.get(portfolio_name)
            if portfolio is None:
                portfolio = PortfolioData(portfolio_name, first.exchange, self.profile_of[first.vt_symbol], state, pos)
                portfolios[portfolio_name] = portfolio
            chain = ChainData(
                chain_id, self.chain_symbol(chain_id), first.exchange, expiry, self.chain_series[chain_id],
                underlying, portfolio,
            )
            portfolio.add_chain(chain)
            chains.append(chain)
        for vt_symbol, contract in self.contracts.items():
            chain_id = self.chain_of_slot[self.slot_index[vt_symbol]]
            if chain_id >= 0:
                chains[chain_id].add_contract(contract, self.slot_index[vt_symbol], self.attributes[vt_symbol])
        return portfolios

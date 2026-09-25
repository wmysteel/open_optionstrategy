"""期权定价模型"""
import numpy as np
from scipy.special import ndtr

SQRT_2PI = np.sqrt(2 * np.pi)


def _pdf(x):
    return np.exp(-0.5 * x * x) / SQRT_2PI


def _european_bounds(f, k, t, r, q, cp):
    """欧式期权无套利区间"""
    forward = f * np.exp(-q * t)
    strike = k * np.exp(-r * t)
    return np.maximum(cp * (forward - strike), 0.0), np.where(cp > 0, forward, strike)


class _Model:
    """定价模型基类"""

    def price(self, f, k, t, r, v, cp):
        raise NotImplementedError

    def greeks(self, f, k, t, r, v, cp):
        raise NotImplementedError

    def price_bounds(self, f, k, t, r, cp):
        """无套利区间"""
        raise NotImplementedError

    def price_vega(self, f, k, t, r, v, cp):
        """价格与 vega"""
        h = 1e-4
        return self.price(f, k, t, r, v, cp), (self.price(f, k, t, r, v + h, cp) - self.price(f, k, t, r, v - h, cp)) / (2 * h)

    def vol_functions(self, f, k, t, r, cp):
        """随波动率变化的价格函数"""
        return (lambda v: self.price(f, k, t, r, v, cp)), (lambda v: self.price_vega(f, k, t, r, v, cp))

    def implied_vol(self, p, f, k, t, r, cp, v0=None, newton_iters=3, tol=1e-6, bisect_iters=26):
        """反解隐含波动率"""
        arrays = np.broadcast_arrays(*(np.asarray(x, dtype=float) for x in (p, f, k, t, cp)))
        shape = arrays[0].shape
        p, f, k, t, cp = (a.ravel() for a in arrays)
        result = np.full(p.shape, np.nan)
        with np.errstate(all="ignore"):
            lower, upper = self.price_bounds(f, k, t, r, cp)
            ok = np.flatnonzero(np.isfinite(p) & np.isfinite(f) & np.isfinite(k) & (t > 0) & (p > lower) & (p < upper))
            p, f, k, t, cp = p[ok], f[ok], k[ok], t[ok], cp[ok]
            price, price_vega = self.vol_functions(f, k, t, r, cp)
            v = np.full(ok.size, np.nan)
            if v0 is not None:
                start = np.broadcast_to(np.asarray(v0, dtype=float), shape).ravel()[ok]
                v = np.where(start > 0, start, np.nan)
                for _ in range(newton_iters):
                    value, vega = price_vega(v)
                    step = (value - p) / np.where(vega > 1e-8, vega, np.nan)
                    new = np.where(v - step <= 0, v / 2, v - step)
                    new = np.where(np.abs(step) > v / 2, np.where(step > 0, v / 1.5, v * 1.5), new)
                    v = np.where(np.isfinite(step), new, v)
            residual = np.abs(price(v) - p) / np.maximum(p, 1e-12)
            bad = np.flatnonzero(~(residual < tol))
            if bad.size:
                low, high = 1e-4, 5.0
                lo = np.full(bad.size, low)
                hi = np.full(bad.size, high)
                pb = p[bad]
                bad_price, bad_price_vega = self.vol_functions(f[bad], k[bad], t[bad], r, cp[bad])
                for _ in range(bisect_iters):
                    mid = 0.5 * (lo + hi)
                    below = bad_price(mid) < pb
                    lo = np.where(below, mid, lo)
                    hi = np.where(below, hi, mid)
                vb = 0.5 * (lo + hi)
                value, vega = bad_price_vega(vb)
                step = (value - pb) / np.where(vega > 1e-8, vega, np.nan)
                vb = np.where(np.isfinite(step), vb - step, vb)
                v[bad] = np.where((lo > low) & (hi < high), vb, np.nan)
        result[ok] = v
        return result.reshape(shape)


class Black76(_Model):
    """Black-76 期货期权模型"""

    def _d1(self, f, k, t, v):
        return (np.log(f / k) + 0.5 * v * v * t) / (v * np.sqrt(t))

    def price(self, f, k, t, r, v, cp):
        with np.errstate(all="ignore"):
            return self._price(f, k, t, r, v, cp, self._d1(f, k, t, v))

    def _price(self, f, k, t, r, v, cp, d1):
        """由 d1 算价格"""
        d2 = d1 - v * np.sqrt(t)
        return np.exp(-r * t) * cp * (f * ndtr(cp * d1) - k * ndtr(cp * d2))

    def price_bounds(self, f, k, t, r, cp):
        """无套利区间"""
        return _european_bounds(f, k, t, r, r, cp)

    def price_vega(self, f, k, t, r, v, cp):
        with np.errstate(all="ignore"):
            d1 = self._d1(f, k, t, v)
            return self._price(f, k, t, r, v, cp, d1), np.exp(-r * t) * f * _pdf(d1) * np.sqrt(t)

    def vol_functions(self, f, k, t, r, cp):
        """随波动率变化的价格函数"""
        log_fk, sqt, disc = np.log(f / k), np.sqrt(t), np.exp(-r * t)
        disc_cp, disc_f = disc * cp, disc * f

        def price_d1(v):
            d1 = (log_fk + 0.5 * v * v * t) / (v * sqt)
            return disc_cp * (f * ndtr(cp * d1) - k * ndtr(cp * (d1 - v * sqt))), d1

        def price_vega(v):
            value, d1 = price_d1(v)
            return value, disc_f * _pdf(d1) * sqt

        return (lambda v: price_d1(v)[0]), price_vega

    def greeks(self, f, k, t, r, v, cp):
        """单位希腊值"""
        with np.errstate(all="ignore"):
            sqt = np.sqrt(t)
            d1 = self._d1(f, k, t, v)
            disc = np.exp(-r * t)
            pdf = _pdf(d1)
            return {
                "delta": disc * cp * ndtr(cp * d1),
                "gamma": disc * pdf / (f * v * sqt),
                "vega": disc * f * pdf * sqt,
                "theta": -disc * f * pdf * v / (2 * sqt) + r * self._price(f, k, t, r, v, cp, d1),
            }


class BlackScholes(_Model):
    """Black-Scholes-Merton 模型"""

    def __init__(self, q: float = 0.0) -> None:
        self.q = q

    def _d1(self, f, k, t, r, v):
        return (np.log(f / k) + (r - self.q + 0.5 * v * v) * t) / (v * np.sqrt(t))

    def price(self, f, k, t, r, v, cp):
        with np.errstate(all="ignore"):
            return self._price(f, k, t, r, v, cp, self._d1(f, k, t, r, v))

    def _price(self, f, k, t, r, v, cp, d1):
        """由 d1 算价格"""
        d2 = d1 - v * np.sqrt(t)
        return cp * (f * np.exp(-self.q * t) * ndtr(cp * d1) - k * np.exp(-r * t) * ndtr(cp * d2))

    def price_bounds(self, f, k, t, r, cp):
        return _european_bounds(f, k, t, r, self.q, cp)

    def price_vega(self, f, k, t, r, v, cp):
        with np.errstate(all="ignore"):
            d1 = self._d1(f, k, t, r, v)
            return self._price(f, k, t, r, v, cp, d1), f * np.exp(-self.q * t) * _pdf(d1) * np.sqrt(t)

    def greeks(self, f, k, t, r, v, cp):
        with np.errstate(all="ignore"):
            sqt = np.sqrt(t)
            d1 = self._d1(f, k, t, r, v)
            d2 = d1 - v * sqt
            dq, dr = np.exp(-self.q * t), np.exp(-r * t)
            pdf = _pdf(d1)
            return {
                "delta": cp * dq * ndtr(cp * d1),
                "gamma": dq * pdf / (f * v * sqt),
                "vega": f * dq * pdf * sqt,
                "theta": -f * dq * pdf * v / (2 * sqt) - cp * r * k * dr * ndtr(cp * d2) + cp * self.q * f * dq * ndtr(cp * d1),
            }


class BinomialTree(_Model):
    """CRR 二叉树模型；futures 为真时标的是期货（风险中性下期货价不漂移），否则是分红率为 q 的现货"""

    def __init__(self, steps: int = 100, q: float = 0.0, american: bool = True, futures: bool = False) -> None:
        self.steps = steps
        self.q = q
        self.american = american
        self.futures = futures

    def _tree(self, f, k, t, r, v, cp):
        """倒推整棵树，返回第 0、1、2 步各节点的价值与每步的涨幅 u、步长 dt"""
        n = self.steps
        dt = t / n
        u = np.exp(v * np.sqrt(dt))
        d = 1 / u
        growth = 1.0 if self.futures else np.exp((r - self.q) * dt)
        p = (growth - d) / (u - d)
        disc = np.exp(-r * dt)
        spot = f[..., None] * u[..., None] ** (2 * np.arange(n + 1) - n)
        value = np.maximum(cp[..., None] * (spot - k[..., None]), 0.0)
        nodes = {}
        for step in range(n - 1, -1, -1):
            value = disc[..., None] * (p[..., None] * value[..., 1:step + 2] + (1 - p[..., None]) * value[..., :step + 1])
            if self.american:
                spot = f[..., None] * u[..., None] ** (2 * np.arange(step + 1) - step)
                value = np.maximum(value, cp[..., None] * (spot - k[..., None]))
            if step <= 2:
                nodes[step] = value
        return nodes, u, dt

    def price(self, f, k, t, r, v, cp):
        f, k, t, v, cp = np.broadcast_arrays(*(np.asarray(x, dtype=float) for x in (f, k, t, v, cp)))
        with np.errstate(all="ignore"):
            result = self._tree(f, k, t, r, v, cp)[0][0][..., 0]
        return np.where(t > 0, np.where(np.isfinite(v) & (v > 0), result, np.nan), np.maximum(cp * (f - k), 0.0))

    def price_bounds(self, f, k, t, r, cp):
        """无套利区间"""
        if self.american:
            return np.maximum(cp * (f - k), 0.0), np.where(cp > 0, f, k)
        return _european_bounds(f, k, t, r, r if self.futures else self.q, cp)

    def greeks(self, f, k, t, r, v, cp):
        """delta、gamma、theta 取树的前两步节点（对整棵树的价格做差分，gamma 会随节点跨过行权价跳动）；vega 重算价格"""
        f, k, t, v, cp = np.broadcast_arrays(*(np.asarray(x, dtype=float) for x in (f, k, t, v, cp)))
        dv = 0.01
        with np.errstate(all="ignore"):
            nodes, u, dt = self._tree(f, k, t, r, v, cp)
            up, down = f * u, f / u
            delta_up = (nodes[2][..., 2] - nodes[2][..., 1]) / (f * u * u - f)
            delta_down = (nodes[2][..., 1] - nodes[2][..., 0]) / (f - f / (u * u))
            return {
                "delta": (nodes[1][..., 1] - nodes[1][..., 0]) / (up - down),
                "gamma": (delta_up - delta_down) / (0.5 * (f * u * u - f / (u * u))),
                "vega": (self.price(f, k, t, r, v + dv, cp) - self.price(f, k, t, r, v - dv, cp)) / (2 * dv),
                "theta": (nodes[2][..., 1] - nodes[0][..., 0]) / (2 * dt),
            }


def iv_price_source(bid, ask, last_trade, last_trade_ts, now_ts, stale_seconds, max_spread):
    """IV 与盯市用的期权价格"""
    two_sided = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask >= bid) & (ask - bid <= max_spread * (bid + ask) / 2)
    fresh = np.isfinite(last_trade) & (now_ts - last_trade_ts <= stale_seconds)
    return np.where(two_sided, (bid + ask) / 2, np.where(fresh, last_trade, np.nan))

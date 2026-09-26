"""Market data for analytics and the copy simulator: asset meta, multi-resolution price bars, funding, regime."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from hl_scout.analytics import Action, EquityCurve, Trip
from hl_scout.hl.client import INTERVAL_MS
from hl_scout.util import HOUR

# Spot tokens bridged to HyperCore under a different name than the perp (heuristic for the hedge detector only).
SPOT_ALIASES: dict[str, str] = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL", "UFART": "FARTCOIN", "UPUMP": "PUMP"}


@dataclass(frozen=True)
class CoinMeta:
    name: str
    sz_decimals: int
    max_leverage: int
    day_volume: float = 0.0
    mark_px: float = float("nan")
    only_isolated: bool = False
    delisted: bool = False

    @property
    def mm_rate(self) -> float:
        """Maintenance margin = half of the initial margin at max leverage [api_notes §7]."""
        return 1.0 / (2.0 * max(self.max_leverage, 1))


def parse_meta(meta_and_ctxs: Any) -> dict[str, CoinMeta]:
    out: dict[str, CoinMeta] = {}
    if not (isinstance(meta_and_ctxs, list) and len(meta_and_ctxs) == 2):
        return out
    meta, ctxs = meta_and_ctxs
    for i, u in enumerate((meta or {}).get("universe", [])):
        ctx = ctxs[i] if isinstance(ctxs, list) and i < len(ctxs) else {}

        def f(key: str, ctx: dict[str, Any] = ctx) -> float:
            try:
                return float(ctx.get(key))
            except (TypeError, ValueError):
                return float("nan")

        out[u["name"]] = CoinMeta(
            name=u["name"],
            sz_decimals=int(u.get("szDecimals", 0)),
            max_leverage=int(u.get("maxLeverage", 1)),
            day_volume=f("dayNtlVlm") if math.isfinite(f("dayNtlVlm")) else 0.0,
            mark_px=f("markPx"),
            only_isolated=bool(u.get("onlyIsolated")) or u.get("marginMode") in ("strictIsolated", "noCross"),
            delisted=bool(u.get("isDelisted")),
        )
    return out


def parse_spot_prices(spot_meta_and_ctxs: Any) -> dict[str, float]:
    """Base token name → mark price in USDC, perp-aliased where a bridged token has another name."""
    out: dict[str, float] = {}
    if not (isinstance(spot_meta_and_ctxs, list) and len(spot_meta_and_ctxs) == 2):
        return out
    meta, ctxs = spot_meta_and_ctxs
    tokens = {t.get("index"): t.get("name") for t in (meta or {}).get("tokens", [])}
    ctx_by_coin = {c.get("coin"): c for c in ctxs or [] if isinstance(c, dict)}
    for pair in (meta or {}).get("universe", []):
        toks = pair.get("tokens") or []
        if len(toks) != 2 or toks[1] != 0:  # quote must be USDC (token index 0)
            continue
        ctx = ctx_by_coin.get(pair.get("name"), {})
        try:
            px = float(ctx.get("markPx") or ctx.get("midPx"))
        except (TypeError, ValueError):
            continue
        name = str(tokens.get(toks[0]))
        out[SPOT_ALIASES.get(name, name)] = px
    return out


class Bars:
    """Price bars of one coin merged across resolutions: the finest available interval wins.

    Only the most recent 5000 candles of each interval exist [api_notes §3], so 1m covers the last days,
    15m the last weeks and 1h the rest.
    """

    def __init__(self, t0: np.ndarray, t1: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray):
        self.t0, self.t1, self.o, self.h, self.l, self.c = t0, t1, o, h, lo, c
        # control points for linear interpolation open → close inside each bar (and across gaps)
        pts_t = np.empty(2 * len(t0), dtype=np.int64)
        pts_p = np.empty(2 * len(t0), dtype=float)
        pts_t[0::2], pts_t[1::2] = t0, np.maximum(t1 - 1, t0)
        pts_p[0::2], pts_p[1::2] = o, c
        self._pt, self._pp = pts_t, pts_p
        self._pt_list: list[int] = pts_t.tolist()  # scalar lookups via bisect are ~10x faster than np.interp
        self._pp_list: list[float] = pts_p.tolist()
        self._sigma: float | None = None

    def __len__(self) -> int:
        return len(self.t0)

    @classmethod
    def merge(cls, series: dict[str, list[tuple]]) -> Bars:
        """`series`: interval → [(t, o, h, l, c, ...)] sorted by t."""
        rows: list[tuple[int, int, float, float, float, float]] = []
        for interval in sorted(series, key=lambda i: -INTERVAL_MS[i]):  # coarsest first
            step = INTERVAL_MS[interval]
            data = [
                (int(r[0]), int(r[0]) + step, float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in series[interval]
            ]
            if not data:
                continue
            finer_start = data[0][0]
            rows = [r for r in rows if r[1] <= finer_start] + data
        if not rows:
            z = np.zeros(0)
            return cls(z.astype(np.int64), z.astype(np.int64), z, z, z, z)
        arr = np.array(rows, dtype=float)
        return cls(arr[:, 0].astype(np.int64), arr[:, 1].astype(np.int64), arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5])

    @property
    def start(self) -> int:
        return int(self.t0[0]) if len(self.t0) else 0

    @property
    def end(self) -> int:
        return int(self.t1[-1]) if len(self.t1) else 0

    def price_at(self, t: float) -> float:
        pt, pp = self._pt_list, self._pp_list
        if not pt:
            return float("nan")
        i = bisect.bisect_right(pt, t)
        if i <= 0:
            return pp[0]
        if i >= len(pt):
            return pp[-1]
        t_a, t_b = pt[i - 1], pt[i]
        if t_b == t_a:
            return pp[i]
        return pp[i - 1] + (pp[i] - pp[i - 1]) * (t - t_a) / (t_b - t_a)

    def prices_at(self, ts: np.ndarray) -> np.ndarray:
        if not len(self._pt):
            return np.full(len(ts), np.nan)
        return np.interp(ts, self._pt, self._pp)

    def resolution_at(self, t: float) -> int:
        if not len(self.t0):
            return HOUR
        i = min(bisect.bisect_right(self._pt_list, t) // 2, len(self.t0) - 1)
        return int(self.t1[i] - self.t0[i])

    def overlapping(self, ta: float, tb: float) -> slice:
        """Bars with t0 < tb and t1 > ta."""
        i0 = int(np.searchsorted(self.t1, ta, side="right"))
        i1 = int(np.searchsorted(self.t0, tb, side="left"))
        return slice(i0, max(i0, i1))

    def extremes(self, ta: float, tb: float) -> tuple[float, float]:
        """(low, high) over [ta, tb]. Edge bars only partly inside the window contribute the interpolated
        prices at ta/tb and their close/open, not their full range: the part of a bar before an entry must not
        count as adverse excursion (matters for 1h bars)."""
        pa, pb = self.price_at(ta), self.price_at(tb)
        lo, hi = min(pa, pb), max(pa, pb)
        s = self.overlapping(ta, tb)
        if s.stop <= s.start:
            return lo, hi
        full = (self.t0[s] >= ta) & (self.t1[s] <= tb)
        if full.any():
            lo = min(lo, float(self.l[s][full].min()))
            hi = max(hi, float(self.h[s][full].max()))
        first, last = s.start, s.stop - 1
        if self.t0[first] < ta < self.t1[first]:
            lo, hi = min(lo, self.c[first]), max(hi, self.c[first])
        if self.t0[last] < tb < self.t1[last]:
            lo, hi = min(lo, self.o[last]), max(hi, self.o[last])
        return float(lo), float(hi)

    def sigma_per_sqrt_ms(self) -> float:
        """Robust Parkinson volatility per √ms from the finest bars available (cached)."""
        if self._sigma is not None:
            return self._sigma
        self._sigma = 0.0
        if len(self.t0) < 10:
            return self._sigma
        res = self.t1 - self.t0
        finest = res == res.min()
        h, lo = self.h[finest], self.l[finest]
        ok = (h > 0) & (lo > 0)
        if ok.sum() >= 5:
            var = (np.log(h[ok] / lo[ok]) ** 2) / (4.0 * math.log(2.0))
            self._sigma = float(math.sqrt(np.median(var)) / math.sqrt(float(res.min())))
        return self._sigma


@dataclass
class Funding:
    t: np.ndarray
    rate: np.ndarray

    def between(self, ta: float, tb: float) -> tuple[np.ndarray, np.ndarray]:
        """Funding events with ta < t ≤ tb (hourly [api_notes §7])."""
        i0 = int(np.searchsorted(self.t, ta, side="right"))
        i1 = int(np.searchsorted(self.t, tb, side="right"))
        return self.t[i0:i1], self.rate[i0:i1]


@dataclass
class Regime:
    """Extreme-volatility regime of the reference coin; the quantile is expanding (no look-ahead)."""

    t: np.ndarray  # hour ends
    extreme: np.ndarray  # bool

    def is_extreme(self, t: float) -> bool:
        i = int(np.searchsorted(self.t, t, side="right")) - 1
        return bool(self.extreme[i]) if 0 <= i < len(self.t) else False

    def extreme_intervals(self, ta: int, tb: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        start = None
        for t, ex in zip(self.t, self.extreme, strict=True):
            if t < ta or t > tb:
                continue
            if ex and start is None:
                start = int(t)
            elif not ex and start is not None:
                out.append((start, int(t)))
                start = None
        if start is not None:
            out.append((start, tb))
        return out


def build_regime(hourly: list[tuple], window_h: int = 24, quantile: float = 0.95, min_hist_h: int = 24 * 30) -> Regime:
    if len(hourly) < window_h + 2:
        return Regime(np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool))
    t = np.array([int(r[0]) + HOUR for r in hourly], dtype=np.int64)
    close = np.array([float(r[4]) for r in hourly])
    lr = np.diff(np.log(np.maximum(close, 1e-12)), prepend=np.log(max(close[0], 1e-12)))
    # rolling std of hourly log returns over `window_h`, scaled to the window
    c1 = np.concatenate([[0.0], np.cumsum(lr)])
    c2 = np.concatenate([[0.0], np.cumsum(lr * lr)])
    vol = np.full(len(t), np.nan)
    idx = np.arange(window_h, len(t))
    s1 = c1[idx + 1] - c1[idx + 1 - window_h]
    s2 = c2[idx + 1] - c2[idx + 1 - window_h]
    vol[idx] = np.sqrt(np.maximum(s2 / window_h - (s1 / window_h) ** 2, 0.0)) * math.sqrt(window_h)
    # expanding quantile, refreshed once a day: only past data is used
    extreme = np.zeros(len(t), dtype=bool)
    threshold = np.nan
    for i in range(min_hist_h, len(t)):
        if (i - min_hist_h) % 24 == 0:
            hist = vol[window_h:i]
            hist = hist[np.isfinite(hist)]
            threshold = float(np.quantile(hist, quantile)) if len(hist) > 10 else np.nan
        if np.isfinite(vol[i]) and np.isfinite(threshold):
            extreme[i] = vol[i] > threshold
    return Regime(t, extreme)


@dataclass
class MarketData:
    meta: dict[str, CoinMeta]
    bars: dict[str, Bars]
    funding: dict[str, Funding]
    spot_prices: dict[str, float]
    regime: Regime | None = None

    def has_prices(self, coin: str) -> bool:
        b = self.bars.get(coin)
        return b is not None and len(b) > 0

    def price_at(self, coin: str, t: float) -> float:
        b = self.bars.get(coin)
        return b.price_at(t) if b is not None else float("nan")


def mtm_pnl_curve(
    actions: list[Action], market: MarketData, base: EquityCurve, t0: int, t1: int, step: int = HOUR
) -> tuple[EquityCurve, set[str]]:
    """Hourly mark-to-market PnL of the wallet's perp positions, from its fills and our candles.

    `portfolio` history is too coarse for risk metrics: its allTime window keeps ~100 points (weekly for old
    accounts), and interpolating weekly points smooths returns — Sharpe/Sortino look better than they were and
    drawdowns disappear. Here every hour is marked to the candle price, trades at their fill price, fees and
    hourly funding included. Account value from `portfolio` is only the denominator for returns.

    Requires complete fills since `t0` (the position at `t0` comes from the first fill's startPosition).
    Returns the curve and the coins that had no candles (their PnL is counted only when realized).
    """
    grid = np.arange(t0 - t0 % step + step, t1 + 1, step, dtype=np.int64)
    total = np.zeros(len(grid))
    unpriced: set[str] = set()
    by_coin: dict[str, list[Action]] = {}
    for a in actions:
        if a.t < t1:
            by_coin.setdefault(a.coin, []).append(a)
    for coin, acts in by_coin.items():
        before = [a for a in acts if a.t < t0]
        inside = [a for a in acts if t0 <= a.t < t1]
        pos0 = before[-1].pos_after if before else (inside[0].pos_before if inside else 0.0)
        if not inside and abs(pos0) < 1e-12:
            continue
        bars = market.bars.get(coin)
        a_t = np.array([a.t for a in inside], dtype=np.int64)
        a_px = np.array([a.px for a in inside], dtype=float)
        a_after = np.array([a.pos_after for a in inside], dtype=float)
        a_fee = np.array([a.fee for a in inside], dtype=float)
        if bars is None or not len(bars):
            unpriced.add(coin)
            realized = np.array([a.closed_pnl - a.fee for a in inside], dtype=float)
            j = np.searchsorted(a_t, grid, side="right")
            total += np.concatenate([[0.0], np.cumsum(realized)])[j]
            continue
        m_px = bars.prices_at(grid)
        # merge actions (priced at the fill) and hourly marks (priced at the candle), actions first on ties
        ev_t = np.concatenate([a_t, grid])
        ev_p = np.concatenate([a_px, m_px])
        ev_fee = np.concatenate([a_fee, np.zeros(len(grid))])
        is_act = np.concatenate([np.ones(len(a_t), dtype=bool), np.zeros(len(grid), dtype=bool)])
        order = np.lexsort((~is_act, ev_t))
        ev_t, ev_p, ev_fee, is_act = ev_t[order], ev_p[order], ev_fee[order], is_act[order]
        after = np.concatenate([a_after, np.full(len(grid), np.nan)])[order]
        # size after each event = size after the latest action so far (forward fill from pos0)
        idx = np.where(is_act, np.arange(len(after)), -1)
        np.maximum.accumulate(idx, out=idx)
        size_after = np.where(idx >= 0, after[np.clip(idx, 0, None)], pos0)
        size_before = np.concatenate([[pos0], size_after[:-1]])
        prev_p = np.concatenate([[bars.price_at(t0)], ev_p[:-1]])
        cum = np.cumsum(size_before * (ev_p - prev_p) - ev_fee)
        coin_pnl = cum[~is_act]
        fund = market.funding.get(coin)
        if fund is not None and len(fund.t):
            ft, fr = fund.between(t0, t1)
            if len(ft):
                k = np.searchsorted(a_t, ft, side="right") - 1
                size_f = np.where(k >= 0, a_after[np.clip(k, 0, None)] if len(a_after) else pos0, pos0)
                pay = np.cumsum(-size_f * bars.prices_at(ft) * fr)
                j = np.searchsorted(ft, grid, side="right") - 1
                coin_pnl = coin_pnl + np.where(j >= 0, pay[np.clip(j, 0, None)], 0.0)
        total += np.nan_to_num(coin_pnl)
    av = np.maximum(1.0, 1.0 + total - total.min()) if base.empty else np.interp(grid, base.t, base.av)
    return EquityCurve(grid, av, total, "fills"), unpriced


def fill_trip_market_stats(trips: list[Trip], actions: list[Action], market: MarketData) -> None:
    """MAE from the first entry price and funding received, both from market data."""
    for tr in trips:
        bars = market.bars.get(tr.coin)
        if bars is None or not len(bars) or not tr.first_px:
            continue
        end = tr.t_close if tr.t_close is not None else bars.end
        lo, hi = bars.extremes(tr.t_open, end)
        adverse = (tr.first_px - lo) / tr.first_px if tr.direction > 0 else (hi - tr.first_px) / tr.first_px
        tr.mae = max(0.0, adverse)
        fund = market.funding.get(tr.coin)
        if fund is None or not tr.actions:
            continue
        ft, fr = fund.between(tr.t_open, end)
        if not len(ft):
            continue
        act_t = np.array([actions[i].t for i in tr.actions], dtype=np.int64)
        act_pos = np.array([actions[i].pos_after for i in tr.actions], dtype=float)
        if tr.pre_existing:
            act_pos = np.concatenate([[tr.direction * tr.entry_sz], act_pos])
            act_t = np.concatenate([[tr.t_open - 1], act_t])
        idx = np.searchsorted(act_t, ft, side="right") - 1
        size = np.where(idx >= 0, act_pos[np.clip(idx, 0, None)], 0.0)
        px = bars.prices_at(ft)
        tr.funding = float(np.nansum(-size * px * fr))

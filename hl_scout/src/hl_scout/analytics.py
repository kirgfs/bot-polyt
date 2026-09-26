"""Wallet analytics: fills → trader actions → round trips → positions; portfolio → equity curve and returns.

Pure functions over parsed data; everything takes an explicit time window so the same code serves "now" and
walk-forward folds without look-ahead.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hl_scout.util import DAY, is_perp_coin

# ------------------------------------------------------------------------------------------------------
# Fills
# ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fill:
    t: int
    coin: str
    side: int  # +1 buy (B), -1 sell (A)
    px: float
    sz: float
    start_pos: float  # signed position before this fill [api_notes §2]
    closed_pnl: float
    fee: float  # positive = paid, negative = rebate
    crossed: bool  # taker
    oid: int
    tid: int
    own_liq: bool  # this fill liquidated our wallet
    liq_counterparty: bool  # wallet took the other side of someone else's liquidation
    twap: bool

    @property
    def end_pos(self) -> float:
        return self.start_pos + self.side * self.sz

    @property
    def notional(self) -> float:
        return self.px * self.sz


def parse_fill(raw: dict[str, Any], address: str) -> Fill:
    liq = raw.get("liquidation") or None
    liquidated_user = str((liq or {}).get("liquidatedUser") or "").lower()
    own_liq = bool(liq) and (not liquidated_user or liquidated_user == address.lower())
    return Fill(
        t=int(raw["time"]),
        coin=str(raw["coin"]),
        side=1 if raw.get("side") == "B" else -1,
        px=float(raw["px"]),
        sz=float(raw["sz"]),
        start_pos=float(raw.get("startPosition") or 0.0),
        closed_pnl=float(raw.get("closedPnl") or 0.0),
        fee=float(raw.get("fee") or 0.0) + float(raw.get("builderFee") or 0.0),
        crossed=bool(raw.get("crossed", True)),
        oid=int(raw.get("oid") or 0),
        tid=int(raw.get("tid") or 0),
        own_liq=own_liq,
        liq_counterparty=bool(liq) and bool(liquidated_user) and liquidated_user != address.lower(),
        twap=raw.get("twapId") not in (None, 0),
    )


def split_fills(raw: list[dict[str, Any]], address: str, allow_hip3: bool = False) -> tuple[list[Fill], list[Fill]]:
    """Returns (main-dex perp fills, spot fills). HIP-3 perps are dropped unless allowed."""
    perp: list[Fill] = []
    spot: list[Fill] = []
    for r in raw:
        coin = str(r.get("coin", ""))
        try:
            f = parse_fill(r, address)
        except (KeyError, TypeError, ValueError):
            continue
        if coin.startswith("@") or "/" in coin:
            spot.append(f)
        elif is_perp_coin(coin, allow_hip3):
            perp.append(f)
    perp.sort(key=lambda f: (f.t, f.tid))
    spot.sort(key=lambda f: (f.t, f.tid))
    return perp, spot


def _near_zero(x: float, ref: float) -> bool:
    return abs(x) <= 1e-9 + 1e-7 * abs(ref)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


# ------------------------------------------------------------------------------------------------------
# Trader actions (fills of one order aggregated)
# ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    t: int
    t_last: int
    coin: str
    side: int
    sz: float
    px: float  # VWAP
    pos_before: float
    pos_after: float
    crossed: bool  # any taker fill
    maker_share: float
    closed_pnl: float
    fee: float
    own_liq: bool
    n_fills: int
    trader_equity: float = 0.0  # trader's account value at the action ("Target Balance"); 0 = unknown

    @property
    def notional(self) -> float:
        return self.sz * self.px

    @property
    def kind(self) -> str:
        ref = max(abs(self.pos_before), abs(self.pos_after), self.sz)
        before_zero = _near_zero(self.pos_before, ref)
        after_zero = _near_zero(self.pos_after, ref)
        if before_zero and after_zero:
            return "noop"
        if before_zero:
            return "open"
        if after_zero:
            return "close"
        if _sign(self.pos_before) != _sign(self.pos_after):
            return "flip"
        return "increase" if abs(self.pos_after) > abs(self.pos_before) else "reduce"


def aggregate_actions(fills: list[Fill], window_ms: int = 2000) -> list[Action]:
    """Group consecutive fills of the same order (oid) within `window_ms` into one trader action."""
    by_coin: dict[str, list[Fill]] = defaultdict(list)
    for f in fills:
        by_coin[f.coin].append(f)
    actions: list[Action] = []
    for coin, cf in by_coin.items():
        group: list[Fill] = []

        def flush(g: list[Fill], coin: str = coin) -> None:
            if not g:
                return
            sz = sum(x.sz for x in g)
            if sz <= 0:
                return
            px = sum(x.px * x.sz for x in g) / sz
            maker_sz = sum(x.sz for x in g if not x.crossed)
            actions.append(
                Action(
                    t=g[0].t,
                    t_last=g[-1].t,
                    coin=coin,
                    side=g[0].side,
                    sz=sz,
                    px=px,
                    pos_before=g[0].start_pos,
                    pos_after=g[-1].end_pos,
                    crossed=any(x.crossed for x in g),
                    maker_share=maker_sz / sz,
                    closed_pnl=sum(x.closed_pnl for x in g),
                    fee=sum(x.fee for x in g),
                    own_liq=any(x.own_liq for x in g),
                    n_fills=len(g),
                )
            )

        for f in cf:
            if group and (f.oid != group[0].oid or f.side != group[0].side or f.t - group[0].t > window_ms):
                flush(group)
                group = []
            group.append(f)
        flush(group)
    actions.sort(key=lambda a: (a.t, a.coin))
    return actions


# ------------------------------------------------------------------------------------------------------
# Round trips
# ------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Trip:
    coin: str
    direction: int  # +1 long, -1 short
    t_open: int
    t_close: int | None = None  # None = still open at the end of the data
    pre_existing: bool = False  # incomplete: opened before our data starts, or its close is missing (gap)
    entry_sz: float = 0.0
    entry_cost: float = 0.0  # Σ px·sz of opening + increasing fills
    exit_sz: float = 0.0
    exit_value: float = 0.0
    max_size: float = 0.0
    first_px: float = 0.0
    first_notional: float = 0.0
    closed_pnl: float = 0.0
    fees: float = 0.0
    n_increases: int = 0
    n_reduces: int = 0
    loss_adds: int = 0
    loss_add_sizes: list[float] = field(default_factory=list)
    loss_add_times: list[int] = field(default_factory=list)
    small_adds: int = 0
    actions: list[int] = field(default_factory=list)  # indexes into the action list
    maker_open: bool = False
    mae: float | None = None  # max adverse excursion from first entry, fraction of price (filled by market data)
    funding: float = 0.0  # filled by market data (positive = received)

    @property
    def closed(self) -> bool:
        return self.t_close is not None

    @property
    def entry_px(self) -> float:
        return self.entry_cost / self.entry_sz if self.entry_sz else self.first_px

    @property
    def exit_px(self) -> float:
        return self.exit_value / self.exit_sz if self.exit_sz else float("nan")

    @property
    def pnl(self) -> float:
        """Realized PnL net of fees (funding excluded; see `pnl_with_funding`)."""
        return self.closed_pnl - self.fees

    @property
    def pnl_with_funding(self) -> float:
        return self.pnl + self.funding

    @property
    def hold_ms(self) -> int:
        return (self.t_close or self.t_open) - self.t_open

    @property
    def max_notional(self) -> float:
        return self.max_size * self.entry_px


def build_trips(actions: list[Action], adverse_pct: float = 0.01, small_add_frac: float = 0.25) -> list[Trip]:
    """Round trips per coin: flat → position → flat (a flip closes one trip and opens the next)."""
    open_trips: dict[str, Trip] = {}
    trips: list[Trip] = []

    def start(i: int, a: Action, direction: int, size: float, px: float, pre: bool = False) -> Trip:
        tr = Trip(coin=a.coin, direction=direction, t_open=a.t, pre_existing=pre)
        tr.entry_sz, tr.entry_cost, tr.max_size = size, size * px, size
        tr.first_px, tr.first_notional = px, size * px
        tr.maker_open = a.maker_share > 0.5
        tr.actions.append(i)
        open_trips[a.coin] = tr
        trips.append(tr)
        return tr

    for i, a in enumerate(actions):
        kind = a.kind
        tr = open_trips.get(a.coin)
        if tr is None and kind in ("increase", "reduce", "close", "flip"):
            # position opened before our history starts: track it but mark as incomplete
            tr = start(i, a, _sign(a.pos_before), abs(a.pos_before), a.px, pre=True)
            tr.actions.pop()
        if kind == "open":
            if tr is not None:
                # a close is missing from the data (gap): the old trip's result is unknown
                tr.t_close, tr.pre_existing = a.t, True
            start(i, a, _sign(a.pos_after), a.sz, a.px).fees += a.fee
        elif kind == "increase" and tr is not None:
            adverse = (tr.entry_px - a.px) / tr.entry_px * tr.direction if tr.entry_px else 0.0
            if adverse >= adverse_pct:
                tr.loss_adds += 1
                tr.loss_add_sizes.append(a.sz)
                tr.loss_add_times.append(a.t)
            if abs(a.pos_before) > 0 and a.sz < small_add_frac * abs(a.pos_before):
                tr.small_adds += 1
            tr.entry_sz += a.sz
            tr.entry_cost += a.sz * a.px
            tr.max_size = max(tr.max_size, abs(a.pos_after))
            tr.n_increases += 1
            tr.fees += a.fee
            tr.actions.append(i)
        elif kind in ("reduce", "close") and tr is not None:
            tr.exit_sz += a.sz
            tr.exit_value += a.sz * a.px
            tr.closed_pnl += a.closed_pnl
            tr.fees += a.fee
            tr.actions.append(i)
            if kind == "reduce":
                tr.n_reduces += 1
            else:
                tr.t_close = a.t
                open_trips.pop(a.coin, None)
        elif kind == "flip" and tr is not None:
            close_sz = abs(a.pos_before)
            frac = close_sz / a.sz if a.sz else 1.0
            tr.exit_sz += close_sz
            tr.exit_value += close_sz * a.px
            tr.closed_pnl += a.closed_pnl
            tr.fees += a.fee * frac
            tr.t_close = a.t
            tr.actions.append(i)
            open_trips.pop(a.coin, None)
            new = start(i, a, _sign(a.pos_after), abs(a.pos_after), a.px)
            new.fees += a.fee * (1 - frac)
    return trips


def closed_trips(trips: list[Trip], t0: int, t1: int) -> list[Trip]:
    """Complete (not pre-existing) trips closed inside [t0, t1)."""
    return [tr for tr in trips if tr.closed and not tr.pre_existing and t0 <= (tr.t_close or 0) < t1]


# ------------------------------------------------------------------------------------------------------
# Positions over time
# ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionPoint:
    t: int
    coin: str
    size: float  # signed, after the action
    px: float


def position_points(actions: list[Action]) -> list[PositionPoint]:
    return [PositionPoint(a.t, a.coin, a.pos_after, a.px) for a in actions]


def concurrency_profile(actions: list[Action], t0: int, t1: int) -> tuple[float, float, float]:
    """(median, p95, share of time in market) of the number of open positions, time-weighted over [t0, t1)."""
    state: dict[str, float] = {}
    for a in actions:  # positions entering the window
        if a.t >= t0:
            break
        state[a.coin] = a.pos_after
    times: list[int] = []
    counts: list[int] = []
    cur_t = t0
    for a in actions:
        if a.t < t0:
            continue
        if a.t >= t1:
            break
        n = sum(1 for v in state.values() if not _near_zero(v, 1.0))
        times.append(a.t - cur_t)
        counts.append(n)
        cur_t = a.t
        state[a.coin] = a.pos_after
    n = sum(1 for v in state.values() if not _near_zero(v, 1.0))
    times.append(max(0, t1 - cur_t))
    counts.append(n)
    w = np.array(times, dtype=float)
    c = np.array(counts, dtype=float)
    in_mkt = c > 0
    if w.sum() <= 0 or not in_mkt.any() or w[in_mkt].sum() <= 0:
        return 0.0, 0.0, 0.0
    return (
        weighted_quantile(c[in_mkt], w[in_mkt], 0.5),
        weighted_quantile(c[in_mkt], w[in_mkt], 0.95),
        float(w[in_mkt].sum() / w.sum()),
    )


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cw = np.cumsum(w)
    if cw[-1] <= 0:
        return float("nan")
    idx = int(np.searchsorted(cw, q * cw[-1], side="left"))
    return float(v[min(idx, len(v) - 1)])


# ------------------------------------------------------------------------------------------------------
# Equity curve from `portfolio` [api_notes §2]
# ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityCurve:
    t: np.ndarray  # ms
    av: np.ndarray  # account value
    pnl: np.ndarray  # cumulative PnL (deposits/withdrawals excluded)
    basis: str  # "perp" or "total"

    @property
    def empty(self) -> bool:
        return len(self.t) < 2

    @property
    def start(self) -> int:
        return int(self.t[0]) if len(self.t) else 0

    def av_at(self, t: float) -> float:
        return float(np.interp(t, self.t, self.av)) if len(self.t) else float("nan")

    def pnl_at(self, t: float) -> float:
        return float(np.interp(t, self.t, self.pnl)) if len(self.t) else float("nan")

    def window(self, t0: int, t1: int) -> EquityCurve:
        """Points inside [t0, t1] plus interpolated end points (no data after t1 is used)."""
        mask = (self.t > t0) & (self.t < t1)
        ts = np.concatenate([[t0], self.t[mask], [t1]]).astype(np.int64)
        if len(self.t) and t0 < self.t[0]:
            ts = ts[ts >= self.t[0]]
        if len(self.t) and t1 > self.t[-1]:
            ts = ts[ts <= self.t[-1]]
        return EquityCurve(ts, np.interp(ts, self.t, self.av), np.interp(ts, self.t, self.pnl), self.basis)

    def point_returns(self, min_av: float = 1.0) -> np.ndarray:
        """Time-weighted return between consecutive points: ΔPnL / account value at the previous point."""
        if len(self.t) < 2:
            return np.zeros(0)
        prev_av = self.av[:-1]
        dp = np.diff(self.pnl)
        return np.where(prev_av > min_av, dp / np.maximum(prev_av, min_av), 0.0)

    def max_drawdown(self, t0: int, t1: int) -> float:
        w = self.window(t0, t1)
        r = w.point_returns()
        if len(r) == 0:
            return 0.0
        idx = np.cumprod(1.0 + np.clip(r, -0.999, None))
        idx = np.concatenate([[1.0], idx])
        peak = np.maximum.accumulate(idx)
        return float(np.max(1.0 - idx / peak))

    def daily_returns(self, t0: int, t1: int) -> np.ndarray:
        """Daily returns on day boundaries t0, t0+1d, … < t1 (interpolated between points)."""
        n_days = int((t1 - t0) // DAY)
        if n_days <= 0 or self.empty:
            return np.zeros(0)
        edges = t0 + DAY * np.arange(n_days + 1)
        edges = edges[(edges >= self.t[0]) & (edges <= self.t[-1])]
        if len(edges) < 2:
            return np.zeros(0)
        av = np.interp(edges, self.t, self.av)
        pnl = np.interp(edges, self.t, self.pnl)
        prev = av[:-1]
        return np.where(prev > 1.0, np.diff(pnl) / np.maximum(prev, 1.0), 0.0)

    def period_pnl(self, t0: int, t1: int) -> float:
        return self.pnl_at(t1) - self.pnl_at(t0)

    def covers(self, t0: int) -> bool:
        return not self.empty and self.t[0] <= t0 + DAY


def _parse_history(pairs: list[Any]) -> tuple[np.ndarray, np.ndarray]:
    ts, vs = [], []
    for p in pairs or []:
        try:
            ts.append(int(p[0]))
            vs.append(float(p[1]))
        except (TypeError, ValueError, IndexError):
            continue
    order = np.argsort(ts, kind="stable")
    return np.array(ts, dtype=np.int64)[order], np.array(vs, dtype=float)[order]


def _window_series(data: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t_av, av = _parse_history(data.get("accountValueHistory", []))
    t_pnl, pnl = _parse_history(data.get("pnlHistory", []))
    if len(t_av) == 0 or len(t_pnl) == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0)
    ts = np.union1d(t_av, t_pnl)
    return ts, np.interp(ts, t_av, av), np.interp(ts, t_pnl, pnl)


def build_equity_curve(portfolio: Any, prefer: str = "perp") -> EquityCurve:
    """Stitch allTime (coarse) with month/week/day (finer) windows. PnL of a finer window is offset to match."""
    windows: dict[str, dict[str, Any]] = {}
    if isinstance(portfolio, list):
        for item in portfolio:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
                windows[str(item[0])] = item[1]

    def stitched(prefix: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        names = ["allTime", "month", "week", "day"]
        if prefix:
            names = [prefix + n[0].upper() + n[1:] for n in names]
        t = np.zeros(0, dtype=np.int64)
        av = np.zeros(0)
        pnl = np.zeros(0)
        for name in names:
            if name not in windows:
                continue
            wt, wav, wpnl = _window_series(windows[name])
            if len(wt) == 0:
                continue
            if len(t):
                offset = float(np.interp(wt[0], t, pnl)) - wpnl[0] if wt[0] <= t[-1] else pnl[-1] - wpnl[0]
                keep = t < wt[0]
                t, av, pnl = t[keep], av[keep], pnl[keep]
                wpnl = wpnl + offset
            t = np.concatenate([t, wt])
            av = np.concatenate([av, wav])
            pnl = np.concatenate([pnl, wpnl])
        return t, av, pnl

    perp = stitched("perp")
    total = stitched("")
    use_perp = prefer == "perp" and len(perp[0]) >= 2
    if use_perp and len(total[0]) >= 2:
        # unified/portfolio-margin accounts keep collateral in spot: perp account value then understates capital
        perp_av = float(np.median(perp[1])) if len(perp[1]) else 0.0
        total_av = float(np.median(total[1])) if len(total[1]) else 0.0
        if total_av > 0 and perp_av < 0.5 * total_av:
            use_perp = False
    t, av, pnl = perp if use_perp else total
    return EquityCurve(t, av, pnl, "perp" if use_perp else "total")


def total_account_value(portfolio: Any) -> float:
    """Latest total account value (allTime window) — used for the equity range filter."""
    curve = build_equity_curve(portfolio, prefer="total")
    return float(curve.av[-1]) if len(curve.av) else 0.0


def portfolio_volume(portfolio: Any, window: str) -> float:
    if isinstance(portfolio, list):
        for item in portfolio:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == window:
                try:
                    return float(item[1].get("vlm") or 0.0)
                except (TypeError, ValueError, AttributeError):
                    return 0.0
    return 0.0


# ------------------------------------------------------------------------------------------------------
# Period statistics
# ------------------------------------------------------------------------------------------------------


def block_pnls(curve: EquityCurve, t_end: int, block_days: int, n_blocks: int) -> list[float]:
    """PnL of consecutive blocks ending at t_end, oldest first."""
    out = []
    for k in range(n_blocks, 0, -1):
        a = t_end - k * block_days * DAY
        b = a + block_days * DAY
        out.append(curve.period_pnl(a, b) if curve.covers(a) else float("nan"))
    return out


def weekly_returns(daily: np.ndarray) -> np.ndarray:
    """Compound daily returns into 7-day blocks aligned to the END of the series (latest week complete)."""
    n = len(daily) // 7
    if n == 0:
        return np.zeros(0)
    tail = daily[len(daily) - 7 * n :]
    return np.prod(1.0 + tail.reshape(n, 7), axis=1) - 1.0


def effective_leverage_samples(actions: list[Action], curve: EquityCurve, t0: int, t1: int) -> np.ndarray:
    """Gross notional / account value right after each action inside [t0, t1)."""
    return effective_leverage_series(actions, curve, t0, t1)[1]


def effective_leverage_series(
    actions: list[Action], curve: EquityCurve, t0: int, t1: int
) -> tuple[np.ndarray, np.ndarray]:
    """(times, gross notional / account value) right after each action inside [t0, t1)."""
    last_px: dict[str, float] = {}
    pos: dict[str, float] = {}
    ts, out = [], []
    for a in actions:
        if a.t >= t1:
            break
        pos[a.coin] = a.pos_after
        last_px[a.coin] = a.px
        if a.t < t0:
            continue
        gross = sum(abs(p) * last_px[c] for c, p in pos.items())
        av = curve.av_at(a.t)
        if av > 0 and math.isfinite(av):
            ts.append(a.t)
            out.append(gross / av)
    return np.array(ts, dtype=np.int64), np.array(out, dtype=float)

"""ВНИМАНИЕ / ПРЕКРАТИТЬ / ПАУЗА rules. The same code runs in the backtest (on history) and, next stage, in monitor.

Wallet rules look only at the wallet's own data up to the check time; the baseline ("historical norm") comes
from the training window, so the test window never informs its own thresholds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from hl_scout.analytics import closed_trips, effective_leverage_samples, effective_leverage_series
from hl_scout.config import FiltersCfg, RulesCfg
from hl_scout.detectors import opposite_positions_share
from hl_scout.market import MarketData
from hl_scout.scoring import Prepared, approx_liq_distance
from hl_scout.sim import SimResult
from hl_scout.util import DAY, HOUR

WARN, STOP, PAUSE = "ВНИМАНИЕ", "ПРЕКРАТИТЬ", "ПАУЗА"


@dataclass(frozen=True)
class Thresholds:
    copy_dd: float
    dd7_mult: float
    consecutive_losses: int
    style_mult: float
    style_min_trades: int
    new_coin_share: float
    near_liq: float
    inactive_days: float
    withdrawal_share: float
    warn_fraction: float
    dd7_min: float
    hedge_time_share: float = 0.30
    hedge_ratio: float = 0.50

    @classmethod
    def from_config(cls, r: RulesCfg, f: FiltersCfg | None = None) -> Thresholds:
        f = f or FiltersCfg()
        return cls(
            copy_dd=r.copy_dd_stop,
            dd7_mult=r.wallet_dd7_mult,
            consecutive_losses=r.consecutive_losses,
            style_mult=r.style_mult,
            style_min_trades=r.style_min_trades,
            new_coin_share=r.new_coin_share,
            near_liq=r.near_liq,
            inactive_days=r.inactive_days,
            withdrawal_share=r.withdrawal_share,
            warn_fraction=r.warn_fraction,
            dd7_min=r.wallet_dd7_min,
            hedge_time_share=f.hedge.opposite_time_share,
            hedge_ratio=f.hedge.opposite_ratio,
        )

    def tuned(self, copy_dd: float, dd7_mult: float, consecutive_losses: int) -> Thresholds:
        return replace(self, copy_dd=copy_dd, dd7_mult=dd7_mult, consecutive_losses=consecutive_losses)


@dataclass(frozen=True)
class Baseline:
    """The wallet's "normal" behaviour, measured on the training window."""

    dd7_p90: float
    leverage_p75: float
    size_median: float
    trades_per_day: float
    coins: frozenset[str]
    opposite_share: float = 0.0  # share of in-market time with long and short legs of comparable size


@dataclass(frozen=True)
class RuleEvent:
    t: int
    rule: str
    level: str  # WARN / STOP
    value: float
    threshold: float
    text: str


def _twr_index(prep: Prepared, t0: int, t1: int) -> tuple[np.ndarray, np.ndarray]:
    w = prep.curve.window(t0, t1)
    r = w.point_returns()
    idx = np.concatenate([[1.0], np.cumprod(1.0 + np.clip(r, -0.999, None))])
    return w.t, idx


def rolling_dd(times: np.ndarray, idx: np.ndarray, window_ms: int) -> np.ndarray:
    """Drawdown from the trailing-window high at every point."""
    out = np.zeros(len(idx))
    j = 0
    for i in range(len(idx)):
        while times[j] < times[i] - window_ms:
            j += 1
        peak = float(np.max(idx[j : i + 1]))
        out[i] = 1.0 - idx[i] / peak if peak > 0 else 0.0
    return out


def make_baseline(prep: Prepared, t0: int, t1: int, hedge_ratio: float = 0.5) -> Baseline:
    times, idx = _twr_index(prep, t0, t1)
    dd7 = rolling_dd(times, idx, 7 * DAY) if len(idx) > 1 else np.zeros(1)
    lev = effective_leverage_samples(prep.actions, prep.total_curve, t0, t1)
    trips = closed_trips(prep.trips, t0, t1)
    sizes = [t.max_notional for t in trips]
    return Baseline(
        dd7_p90=float(np.quantile(dd7, 0.9)) if len(dd7) else 0.0,
        leverage_p75=float(np.quantile(lev, 0.75)) if len(lev) else 0.0,
        size_median=float(np.median(sizes)) if sizes else 0.0,
        trades_per_day=len(trips) / max((t1 - t0) / DAY, 1e-9),
        coins=frozenset(t.coin for t in trips),
        opposite_share=opposite_positions_share(prep.actions, t0, t1, hedge_ratio),
    )


def _cross(values: np.ndarray, times: np.ndarray, threshold: float, above: bool = True) -> int | None:
    hit = values >= threshold if above else values <= threshold
    if not hit.any():
        return None
    return int(times[int(np.argmax(hit))])


@dataclass
class WalletSignals:
    """Rule inputs for [t_a, t_b), computed once. The tunable thresholds (wallet drawdown multiple, losing
    streak) are applied cheaply for every candidate value when tuning on the training window."""

    t_a: int
    t_b: int
    dd7_p90: float
    dd_times: np.ndarray
    dd_values: np.ndarray
    streaks: list[tuple[int, int]]  # (trip close time, current losing streak)
    fixed: list[RuleEvent]  # rules whose thresholds are not tuned

    def _dd7_events(self, th: Thresholds) -> list[RuleEvent]:
        out: list[RuleEvent] = []
        if not len(self.dd_values):
            return out
        stop_th = max(th.dd7_mult * self.dd7_p90, th.dd7_min)
        peak = float(self.dd_values.max(initial=0))
        t_warn = _cross(self.dd_values, self.dd_times, th.warn_fraction * stop_th)
        t_stop = _cross(self.dd_values, self.dd_times, stop_th)
        if t_warn is not None:
            out.append(
                RuleEvent(
                    t_warn,
                    "wallet_dd7",
                    WARN,
                    peak,
                    th.warn_fraction * stop_th,
                    f"просадка кошелька за 7 дней приближается к порогу {stop_th:.0%}",
                )
            )
        if t_stop is not None:
            out.append(
                RuleEvent(
                    t_stop,
                    "wallet_dd7",
                    STOP,
                    peak,
                    stop_th,
                    f"просадка кошелька за 7 дней > {stop_th:.0%} (норма p90 {self.dd7_p90:.0%})",
                )
            )
        return out

    def _streak_events(self, th: Thresholds) -> list[RuleEvent]:
        out: list[RuleEvent] = []
        n = th.consecutive_losses
        warn_n = max(1, math.ceil(th.warn_fraction * n))
        for t, s in self.streaks:
            if s == warn_n and warn_n < n:
                out.append(RuleEvent(t, "consecutive_losses", WARN, s, n, f"{s} убыточных сделок подряд"))
            if s == n:
                out.append(RuleEvent(t, "consecutive_losses", STOP, s, n, f"{s} убыточных сделок подряд"))
        return out

    def events(self, th: Thresholds) -> list[RuleEvent]:
        ev = [*self.fixed, *self._dd7_events(th), *self._streak_events(th)]
        return sorted((e for e in ev if self.t_a <= e.t < self.t_b), key=lambda e: e.t)


def wallet_events(
    prep: Prepared, market: MarketData, base: Baseline, th: Thresholds, t_a: int, t_b: int
) -> list[RuleEvent]:
    """All WARN/STOP events of the wallet-based rules inside [t_a, t_b)."""
    return wallet_signals(prep, market, base, th, t_a, t_b).events(th)


def wallet_signals(
    prep: Prepared, market: MarketData, base: Baseline, th: Thresholds, t_a: int, t_b: int
) -> WalletSignals:
    ev: list[RuleEvent] = []
    wf = th.warn_fraction

    def add(t: int | None, rule: str, level: str, value: float, threshold: float, text: str) -> None:
        if t is not None and t_a <= t < t_b:
            ev.append(RuleEvent(t, rule, level, value, threshold, text))

    # 1) wallet drawdown over the trailing 7 days (thresholds applied in WalletSignals)
    times, idx = _twr_index(prep, t_a - 7 * DAY, t_b)
    dd_t, dd_v = np.zeros(0, dtype=np.int64), np.zeros(0)
    if len(idx) > 1:
        dd = rolling_dd(times, idx, 7 * DAY)
        in_win = times >= t_a
        dd_t, dd_v = times[in_win], dd[in_win]

    # 2) losing streaks (threshold applied in WalletSignals)
    streaks: list[tuple[int, int]] = []
    streak = 0
    done = sorted(
        (t for t in prep.trips if t.closed and not t.pre_existing and (t.t_close or 0) < t_b),
        key=lambda t: t.t_close or 0,
    )
    for tr in done:
        streak = streak + 1 if tr.pnl_with_funding < 0 else 0
        if tr.t_close is not None and tr.t_close >= t_a:
            streaks.append((tr.t_close, streak))

    # 3) averaging down a losing position
    for tr in prep.trips:
        for t_add in tr.loss_add_times:
            add(t_add, "averaging_down", STOP, tr.loss_adds, 0, f"усреднение убыточной позиции {tr.coin}")

    # 4) style change and hedges at each trader action; 5) near liquidation hourly — one pass, incremental state
    lev_t, lev_v = effective_leverage_series(prep.actions, prep.total_curve, t_a - 7 * DAY, t_b)
    trips_by_open = sorted((t for t in prep.trips if not t.pre_existing), key=lambda t: t.t_open)
    open_times = np.array([t.t_open for t in trips_by_open], dtype=np.int64)
    pos: dict[str, tuple[float, float]] = {}
    i_act = 0
    acts = prep.actions
    hours = list(range(t_a - t_a % HOUR + HOUR, t_b, HOUR))
    checks = sorted([(a.t, 0, k) for k, a in enumerate(acts) if t_a <= a.t < t_b] + [(h, 1, -1) for h in hours])
    liq_stopped = False
    for t, kind, k in checks:
        while i_act < len(acts) and (acts[i_act].t < t or (kind == 0 and i_act <= k)):
            a = acts[i_act]
            if abs(a.pos_after) > 1e-12:
                pos[a.coin] = (a.pos_after, a.px)
            else:
                pos.pop(a.coin, None)
            i_act += 1
        if kind == 0:
            a = acts[k]
            lo, hi = np.searchsorted(open_times, [t - 7 * DAY, t], side="right")
            recent = trips_by_open[lo:hi]
            if len(recent) >= th.style_min_trades:
                _style(add, t, "частота сделок", len(recent) / 7.0, base.trades_per_day, th)
                _style(
                    add, t, "размер позиций", float(np.median([x.max_notional for x in recent])), base.size_median, th
                )
            j0, j1 = np.searchsorted(lev_t, [t - 7 * DAY, t], side="right")
            if j1 - j0 >= th.style_min_trades:
                _style(add, t, "плечо", float(np.quantile(lev_v[j0:j1], 0.75)), base.leverage_p75, th)
            if base.coins and a.coin not in base.coins:
                notional_all = sum(x.max_notional for x in recent) or 1.0
                share = sum(x.max_notional for x in recent if x.coin not in base.coins) / notional_all
                add(t, "new_coin", WARN, share, th.new_coin_share, f"новая монета {a.coin}")
                if share >= th.new_coin_share:
                    add(t, "new_coin", STOP, share, th.new_coin_share, f"{share:.0%} объёма в новых монетах")
            longs = sum(abs(s) * px for s, px in pos.values() if s > 0)
            shorts = sum(abs(s) * px for s, px in pos.values() if s < 0)
            if longs > 0 and shorts > 0 and min(longs, shorts) / max(longs, shorts) >= th.hedge_ratio:
                # one pair of opposite positions is normal for a two-coin trader; hedging as a *habit* is not
                share = opposite_positions_share(acts, t - 7 * DAY, t + 1, th.hedge_ratio)
                limit = max(th.hedge_time_share, base.opposite_share * th.style_mult)
                if share >= th.warn_fraction * limit:
                    add(t, "hedge", WARN, share, limit, f"противоположные позиции {share:.0%} времени за 7 дней")
                if share >= limit:
                    add(t, "hedge", STOP, share, limit, f"появились хеджи: противоположные позиции {share:.0%} времени")
        elif pos and not liq_stopped:
            prices = {c: market.price_at(c, t) for c in pos}
            prices = {c: p for c, p in prices.items() if math.isfinite(p)}
            dist = approx_liq_distance(pos, prep.total_curve.av_at(t), market, prices)
            if dist < th.near_liq / wf:
                add(t, "near_liquidation", WARN, dist, th.near_liq / wf, f"позиция в {dist:.1%} от ликвидации")
            if dist < th.near_liq:
                add(t, "near_liquidation", STOP, dist, th.near_liq, f"позиция в {dist:.1%} от ликвидации")
                liq_stopped = True

    # 6) inactivity: the gap since the last trader action
    last = max((a.t for a in acts if a.t < t_a), default=None)
    for t_next in [*(a.t for a in acts if t_a <= a.t < t_b), t_b]:
        if last is not None:
            t_warn = last + int(wf * th.inactive_days * DAY)
            t_stop = last + int(th.inactive_days * DAY)
            if t_warn < t_next:
                add(
                    t_warn,
                    "inactivity",
                    WARN,
                    wf * th.inactive_days,
                    th.inactive_days,
                    f"нет сделок {wf * th.inactive_days:.1f} дн",
                )
            if t_stop < t_next:
                add(
                    t_stop,
                    "inactivity",
                    STOP,
                    th.inactive_days,
                    th.inactive_days,
                    f"нет сделок {th.inactive_days:g} дн",
                )
        last = t_next

    # 7) withdrawals over 7 days vs equity
    outflows = ledger_outflows(prep.address, prep.data.ledger)
    for t, _ in outflows:
        if not (t_a <= t < t_b):
            continue
        out7 = sum(x for tt, x in outflows if t - 7 * DAY < tt <= t)
        eq = prep.total_curve.av_at(t) + out7
        share = out7 / eq if eq > 0 else 0.0
        if share >= wf * th.withdrawal_share:
            add(t, "withdrawal", WARN, share, th.withdrawal_share, f"вывод {share:.0%} equity за 7 дней")
        if share >= th.withdrawal_share:
            add(t, "withdrawal", STOP, share, th.withdrawal_share, f"вывод {share:.0%} equity за 7 дней")
    return WalletSignals(t_a, t_b, base.dd7_p90, dd_t, dd_v, streaks, ev)


_OUT_TYPES = ("internalTransfer", "subAccountTransfer", "send", "spotTransfer")


def ledger_outflows(address: str, ledger: list[dict]) -> list[tuple[int, float]]:
    """(time, USD) of money leaving the wallet: withdrawals and transfers to other addresses [api_notes §2]."""
    me = address.lower()
    out: list[tuple[int, float]] = []
    for u in ledger:
        d, t = u.get("delta") or {}, int(u.get("time") or 0)
        outgoing = d.get("type") in _OUT_TYPES and str(d.get("user", "")).lower() == me
        outgoing = outgoing and str(d.get("destination", "")).lower() != me
        try:
            if d.get("type") == "withdraw":
                amount = float(d.get("usdc") or 0)
            elif outgoing:
                amount = float(d.get("usdc") or d.get("usdcValue") or 0)
            else:
                amount = 0.0
        except (TypeError, ValueError):
            amount = 0.0
        if amount > 0:
            out.append((t, amount))
    return out


def _style(add, t: int, what: str, now: float, base: float, th: Thresholds) -> None:  # type: ignore[no-untyped-def]
    if base <= 0 or now <= 0:
        return
    ratio = now / base
    warn_at = 1.0 + th.warn_fraction * (th.style_mult - 1.0)
    if ratio >= warn_at:
        add(t, "style", WARN, ratio, warn_at, f"{what} выросло в {ratio:.1f} раза")
    if ratio >= th.style_mult:
        add(t, "style", STOP, ratio, th.style_mult, f"{what} выросло в {ratio:.1f} раза")


def copy_dd_events(res: SimResult, th: Thresholds) -> list[RuleEvent]:
    """My simulated copy lost more than the threshold of the allocated amount."""
    out = []
    for level, frac in ((WARN, th.warn_fraction * th.copy_dd), (STOP, th.copy_dd)):
        t = _cross(res.path_worst, res.path_t, res.start * (1 - frac), above=False)
        if t is not None:
            out.append(RuleEvent(t, "copy_dd", level, frac, frac, f"копия ушла в минус больше {frac:.0%}"))
    return out


@dataclass(frozen=True)
class Stopped:
    end: float
    t_signal: int | None
    t_exit: int | None
    reason: str | None


def apply_stop(res: SimResult, events: list[RuleEvent], reaction_ms: int, exit_cost: float) -> Stopped:
    """I get the STOP message at the first STOP event and switch the bot off `reaction_ms` later."""
    stops = [e for e in events if e.level == STOP and res.t_start <= e.t < res.t_end]
    if not stops:
        return Stopped(res.end, None, None, None)
    first = min(stops, key=lambda e: e.t)
    t_exit = first.t + reaction_ms
    if res.stopped_at is not None and res.stopped_at <= t_exit:
        return Stopped(res.end, first.t, res.stopped_at, "бот остановился сам (Balance SL / ликвидация)")
    if t_exit >= res.t_end:
        return Stopped(res.end, first.t, None, first.text)
    eq = res.equity_at(t_exit)
    notional = float(np.interp(t_exit, res.path_t, res.path_notional))
    return Stopped(max(0.0, eq - notional * exit_cost), first.t, t_exit, first.text)


def stopped_equity_series(res: SimResult, st: Stopped, times: np.ndarray) -> np.ndarray:
    eq = res.equity_series(times)
    if st.t_exit is not None:
        eq = np.where(times >= st.t_exit, st.end, eq)
    return eq

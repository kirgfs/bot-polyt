"""Section 2.5: can the copy bot really repeat this wallet on MY deposit, and do my settings survive its style?"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from hl_scout.analytics import Trip, closed_trips, concurrency_profile
from hl_scout.config import Config
from hl_scout.market import MarketData
from hl_scout.scoring import Prepared
from hl_scout.sim import CopySettings, CopySimulator, SimResult


@dataclass(frozen=True)
class TrainStats:
    """Wallet style on a window, as needed to derive copy-bot settings."""

    n_trips: int
    trip_notional_median: float
    trip_notional_p95: float
    entry_notional_median: float
    mae_p95: float
    concurrency_p95: float
    mm_rate: float  # worst (largest) maintenance margin rate among the wallet's coins
    increases_per_trip: float
    coins: tuple[str, ...]
    long_pnl: float
    short_pnl: float
    equity_median: float = 0.0  # trader's account value at their actions in the window ("Target Balance")


def train_stats(prep: Prepared, market: MarketData, t0: int, t1: int) -> TrainStats:
    trips = closed_trips(prep.trips, t0, t1)
    notionals = np.array([t.max_notional for t in trips], dtype=float)
    entries = np.array([t.first_notional for t in trips], dtype=float)
    maes = _maes(trips)
    _, conc_p95, _ = concurrency_profile(prep.actions, t0, t1)
    coins = tuple(sorted({t.coin for t in trips}))
    mm = max((market.meta[c].mm_rate for c in coins if c in market.meta), default=1.0 / 20)
    return TrainStats(
        n_trips=len(trips),
        trip_notional_median=float(np.median(notionals)) if len(notionals) else 0.0,
        trip_notional_p95=float(np.quantile(notionals, 0.95)) if len(notionals) else 0.0,
        entry_notional_median=float(np.median(entries)) if len(entries) else 0.0,
        mae_p95=float(np.quantile(maes, 0.95)) if len(maes) else float("nan"),
        concurrency_p95=conc_p95,
        mm_rate=mm,
        increases_per_trip=float(np.mean([t.n_increases for t in trips])) if trips else 0.0,
        coins=coins,
        long_pnl=sum(t.pnl_with_funding for t in trips if t.direction > 0),
        short_pnl=sum(t.pnl_with_funding for t in trips if t.direction < 0),
        equity_median=float(np.median(eqs))
        if len(eqs := [a.trader_equity for a in prep.actions if t0 <= a.t < t1 and a.trader_equity > 0])
        else 0.0,
    )


def _maes(trips: list[Trip]) -> np.ndarray:
    return np.array([t.mae for t in trips if t.mae is not None and math.isfinite(t.mae)], dtype=float)


def my_liq_distance(leverage: int, mm_rate: float) -> float:
    """Adverse price move that liquidates an isolated position at this leverage (worst case for cross too)."""
    return max(0.0, 1.0 / max(1, leverage) - mm_rate)


def safe_leverages(ts: TrainStats, levels: list[int], safety: float) -> list[int]:
    """Leverages whose liquidation is farther than the wallet's p95 adverse excursion × safety [2.5]."""
    if not math.isfinite(ts.mae_p95):
        return []
    return [lev for lev in levels if my_liq_distance(lev, ts.mm_rate) >= safety * ts.mae_p95]


@dataclass
class Replicability:
    settings_label: str
    lost_actions: float  # share of entries/adds/partial closes below $10 (skipped or rounded up)
    skipped_by_limits: int
    late_entries: int  # my entry was skipped, a later add of the trader opened my position
    partial_exits_skipped: int
    reduces_bumped: int
    min_size_pnl_impact: float  # my PnL minus the same copy without the $10 minimum
    concurrency_p95: float
    margin_needed: float
    margin_share: float
    ladder_pnl_first_only: float
    ladder_pnl_full: float
    mae_p95: float
    my_liq_distance: float
    liq_cushion: float  # my liquidation distance / wallet p95 MAE
    price_stops: int
    stop_in_vain: float | None  # share of my Price SL exits on trades the wallet closed in profit
    fees: float
    slippage: float
    funding: float
    gross_pnl: float
    cost_share: float
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons


def stop_in_vain(res: SimResult, prep: Prepared) -> tuple[int, float | None]:
    """How often my Price SL closed a position that the wallet later closed in profit."""
    stops = [s for s in res.stops if s.kind == "price_sl" and s.coin]
    if not stops:
        return 0, None
    vain = 0
    for s in stops:
        covering = [t for t in prep.trips if t.coin == s.coin and t.t_open <= s.t <= (t.t_close or s.t)]
        if covering and covering[-1].closed and covering[-1].pnl_with_funding > 0:
            vain += 1
    return len(stops), vain / len(stops)


def assess(
    prep: Prepared,
    market: MarketData,
    sim: CopySimulator,
    s: CopySettings,
    ts: TrainStats,
    t0: int,
    t1: int,
    cfg: Config,
    res: SimResult | None = None,
) -> Replicability:
    rc = cfg.recommend
    res = res or sim.run(prep.actions, s, t0, t1)
    no_min = sim.run(prep.actions, replace(s, min_trade_usd=0.0, small_size="skip"), t0, t1)
    first_only = sim.run(prep.actions, replace(s, buy_times=1), t0, t1)
    o = res.outcomes
    margin_per_pos = (s.target_usd or s.copy_ratio * ts.trip_notional_median) / max(1, s.leverage)
    margin_needed = math.ceil(max(1.0, ts.concurrency_p95)) * margin_per_pos
    liq = my_liq_distance(s.leverage, ts.mm_rate)
    n_stops, vain = stop_in_vain(res, prep)
    gross = res.gross_pnl
    cost_share = res.costs / gross if gross > 0 else float("inf")
    rep = Replicability(
        settings_label=s.label,
        lost_actions=res.lost_action_share(),
        skipped_by_limits=sum(v for k, v in o.items() if k.endswith("skipped_limits")),
        late_entries=o["late_entry_copied"] + o["late_entry_bumped"],
        partial_exits_skipped=o["reduce_skipped_small"],
        reduces_bumped=o["reduce_bumped"],
        min_size_pnl_impact=res.pnl - no_min.pnl,
        concurrency_p95=ts.concurrency_p95,
        margin_needed=margin_needed,
        margin_share=margin_needed / s.alloc_usd,
        ladder_pnl_first_only=first_only.pnl,
        ladder_pnl_full=res.pnl,
        mae_p95=ts.mae_p95,
        my_liq_distance=liq,
        liq_cushion=liq / ts.mae_p95 if ts.mae_p95 and math.isfinite(ts.mae_p95) and ts.mae_p95 > 0 else float("inf"),
        price_stops=n_stops,
        stop_in_vain=vain,
        fees=res.fees,
        slippage=res.slippage_cost,
        funding=res.funding,
        gross_pnl=gross,
        cost_share=cost_share,
    )
    if rep.lost_actions > rc.max_lost_actions:
        rep.reasons.append(
            f"теряется {rep.lost_actions:.0%} действий (ордер < ${s.min_trade_usd:g}), порог {rc.max_lost_actions:.0%}"
        )
    if rep.margin_share > rc.max_margin_share + 1e-9:
        rep.reasons.append(
            f"p95 одновременных позиций требует {rep.margin_share:.0%} депозита (> {rc.max_margin_share:.0%})"
        )
    if rep.liq_cushion < rc.liq_safety:
        rep.reasons.append(f"моя ликвидация ближе p95 просадки кошелька × {rc.liq_safety:g}")
    if vain is not None and vain > rc.max_stop_in_vain:
        rep.reasons.append(f"стоп выбивал бы зря в {vain:.0%} случаев (> {rc.max_stop_in_vain:.0%})")
    if cost_share > rc.max_cost_share:
        share = "валовая прибыль ≤ 0" if not math.isfinite(cost_share) else f"{cost_share:.0%} валовой прибыли"
        rep.reasons.append(f"комиссии, funding и проскальзывание: {share} (порог {rc.max_cost_share:.0%})")
    return rep

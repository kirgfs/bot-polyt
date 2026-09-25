"""Hard filters and the skill score. Everything is evaluated "as of" a time T using only data before T."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hl_scout import detectors as det
from hl_scout.analytics import (
    Action,
    EquityCurve,
    Fill,
    Trip,
    aggregate_actions,
    block_pnls,
    build_equity_curve,
    build_trips,
    closed_trips,
    concurrency_profile,
    effective_leverage_samples,
    portfolio_volume,
    split_fills,
    weekly_returns,
)
from hl_scout.config import Config
from hl_scout.market import MarketData, fill_trip_market_stats
from hl_scout.stats import (
    benjamini_hochberg,
    bootstrap_pvalue,
    clip01,
    deflated_sharpe,
    profit_factor,
    sharpe,
    sortino,
)
from hl_scout.util import DAY, MIN

# ----------------------------------------------------------------------------------------------------------
# Input data
# ----------------------------------------------------------------------------------------------------------


@dataclass
class WalletData:
    """Raw API payloads for one address (from the SQLite cache)."""

    address: str
    raw_fills: list[dict[str, Any]]
    fills_truncated: bool
    portfolio: Any
    clearinghouse: Any | None = None
    spot_state: Any | None = None
    ledger: list[dict[str, Any]] = field(default_factory=list)
    role: dict[str, Any] | None = None
    leaderboard: dict[str, Any] | None = None
    sources: set[str] = field(default_factory=set)


@dataclass
class Prepared:
    """Parsed once per wallet; shared by scoring (any T) and the backtest."""

    address: str
    data: WalletData
    perp_fills: list[Fill]
    spot_fills: list[Fill]
    actions: list[Action]
    trips: list[Trip]
    curve: EquityCurve  # perp (or total for unified accounts), for returns and drawdowns
    total_curve: EquityCurve  # total account value, for the equity range
    coins: set[str]

    @property
    def earliest_fill(self) -> int | None:
        return self.perp_fills[0].t if self.perp_fills else None


def prepare(wd: WalletData, market: MarketData, cfg: Config) -> Prepared:
    perp, spot = split_fills(wd.raw_fills, wd.address, cfg.universe.allow_hip3)
    perp = [f for f in perp if f.coin not in cfg.universe.exclude_coins]
    actions = aggregate_actions(perp, cfg.copying.aggregate_window_ms)
    trips = build_trips(actions, cfg.filters.martingale.adverse_pct)
    fill_trip_market_stats(trips, actions, market)
    return Prepared(
        address=wd.address,
        data=wd,
        perp_fills=perp,
        spot_fills=spot,
        actions=actions,
        trips=trips,
        curve=build_equity_curve(wd.portfolio, prefer="perp"),
        total_curve=build_equity_curve(wd.portfolio, prefer="total"),
        coins={a.coin for a in actions},
    )


# ----------------------------------------------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterResult:
    name: str
    passed: bool
    value: str
    threshold: str
    kind: str = "style"  # data | style | perf — perf filters select on results, so they count as multiple testing


@dataclass
class WalletEval:
    address: str
    t_asof: int
    filters: list[FilterResult]
    metrics: dict[str, float]
    style: dict[str, Any]
    daily: np.ndarray
    weekly: np.ndarray
    # filled by cross-sectional passes
    dsr: float = 0.0
    pvalue: float = 1.0
    qvalue: float = 1.0
    cluster: str = ""
    linked: list[str] = field(default_factory=list)
    copyability: float | None = None
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return all(f.passed for f in self.filters)

    @property
    def failed(self) -> list[FilterResult]:
        return [f for f in self.filters if not f.passed]


_PERF = {"max drawdown", "прибыльные месяцы", "топ-3 сделки"}
_DATA = {"история 90 дней", "полнота филлов"}


def _f(name: str, passed: bool, value: Any, threshold: str) -> FilterResult:
    if isinstance(value, float):
        value = f"{value:.3g}" if math.isfinite(value) else "—"
    kind = "perf" if name in _PERF else "data" if name in _DATA else "style"
    return FilterResult(name, bool(passed), str(value), threshold, kind)


def positions_at(actions: list[Action], t: int) -> dict[str, tuple[float, float]]:
    """coin → (signed size, last px) right before t."""
    out: dict[str, tuple[float, float]] = {}
    for a in actions:
        if a.t >= t:
            break
        out[a.coin] = (a.pos_after, a.px)
    return {c: v for c, v in out.items() if abs(v[0]) > 1e-12}


def approx_liq_distance(
    positions: dict[str, tuple[float, float]], equity: float, market: MarketData, prices: dict[str, float] | None = None
) -> float:
    """Cross-margin estimate of the uniform adverse move that wipes equity down to maintenance margin."""
    gross = mm = 0.0
    for coin, (size, px) in positions.items():
        p = (prices or {}).get(coin, px)
        notional = abs(size) * p
        gross += notional
        meta = market.meta.get(coin)
        mm += notional * (meta.mm_rate if meta else 0.5 / 20)
    if gross <= 0:
        return float("inf")
    return max(0.0, (equity - mm) / gross)


def current_liq_distance(clearinghouse: Any) -> float:
    """Min distance mark → liquidation price across current positions [api_notes §2]."""
    best = float("inf")
    if not isinstance(clearinghouse, dict):
        return best
    for ap in clearinghouse.get("assetPositions") or []:
        p = ap.get("position") or {}
        try:
            szi = abs(float(p.get("szi") or 0.0))
            value = abs(float(p.get("positionValue") or 0.0))
            liq = p.get("liquidationPx")
            liq_px = float(liq) if liq not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if szi <= 0 or value <= 0 or liq_px is None:
            continue
        mark = value / szi
        best = min(best, abs(mark - liq_px) / mark)
    return best


def evaluate(prep: Prepared, market: MarketData, cfg: Config, t_asof: int, *, live: bool) -> WalletEval:
    """Hard filters + metrics as of `t_asof`. `live=True` adds checks that only exist for "now"
    (current positions, spot balances, fill coverage vs the portfolio's month volume)."""
    fc = cfg.filters
    t0 = t_asof - fc.lookback_days * DAY
    trips = closed_trips(prep.trips, t0, t_asof)
    pnls = [t.pnl_with_funding for t in trips]
    acts = [a for a in prep.actions if a.t < t_asof]
    last_action = acts[-1].t if acts else None
    curve = prep.curve
    daily = curve.daily_returns(t0, t_asof)
    weekly = weekly_returns(daily)
    filters: list[FilterResult] = []
    metrics: dict[str, float] = {}

    # --- data sufficiency (fail-closed) ---
    earliest = prep.earliest_fill
    history_ok = curve.covers(t0) and (not prep.data.fills_truncated or (earliest is not None and earliest <= t0))
    filters.append(
        _f("история 90 дней", history_ok, "есть" if history_ok else "неполная", "portfolio и филлы покрывают окно")
    )
    if live:
        month_vlm = portfolio_volume(prep.data.portfolio, "perpMonth")
        fills_vlm = sum(f.notional for f in prep.perp_fills if f.t >= t_asof - 30 * DAY)
        coverage = fills_vlm / month_vlm if month_vlm > 0 else 1.0
        metrics["fill_coverage"] = coverage
        filters.append(
            _f("полнота филлов", coverage >= fc.min_fill_coverage, coverage, f"≥ {fc.min_fill_coverage:.0%}")
        )

    # --- activity ---
    filters.append(_f("сделок за 90 дней", len(trips) >= fc.min_trades, len(trips), f"≥ {fc.min_trades}"))
    active = last_action is not None and last_action >= t_asof - fc.active_within_days * DAY
    days_idle = (t_asof - last_action) / DAY if last_action else float("inf")
    filters.append(_f("активность 7 дней", active, days_idle, f"последняя сделка ≤ {fc.active_within_days:g} дн назад"))
    holds = np.array([t.hold_ms for t in trips], dtype=float)
    avg_hold = float(holds.mean()) if len(holds) else 0.0
    filters.append(
        _f("среднее удержание", avg_hold > fc.min_avg_hold_min * MIN, avg_hold / MIN, f"> {fc.min_avg_hold_min:g} мин")
    )

    # --- risk ---
    mdd = curve.max_drawdown(t0, t_asof) if curve.covers(t0) else float("nan")
    filters.append(_f("max drawdown", math.isfinite(mdd) and mdd < fc.max_drawdown, mdd, f"< {fc.max_drawdown:.0%}"))
    months = block_pnls(curve, t_asof, 30, fc.months_window)
    n_prof = sum(1 for m in months if math.isfinite(m) and m > 0)
    filters.append(
        _f(
            "прибыльные месяцы",
            n_prof >= fc.min_profitable_months,
            f"{n_prof}/{fc.months_window}",
            f"≥ {fc.min_profitable_months}",
        )
    )
    total = sum(pnls)
    top3 = sum(sorted((p for p in pnls if p > 0), reverse=True)[:3])
    top3_share = top3 / total if total > 0 else float("inf")
    filters.append(_f("топ-3 сделки", top3_share < fc.max_top3_share, top3_share, f"< {fc.max_top3_share:.0%} прибыли"))
    mg = det.martingale_stats(trips)
    mg_ok = (
        mg.trip_share <= fc.martingale.max_trip_share
        and mg.max_loss_adds <= fc.martingale.max_loss_adds_in_trip
        and not mg.escalating
    )
    filters.append(
        _f("нет мартингейла", mg_ok, f"{mg.trip_share:.0%} сделок, max {mg.max_loss_adds}", "усреднение убытка")
    )
    own_liq, liq_counter = det.liquidation_stats(prep.perp_fills, t0, t_asof)
    filters.append(_f("собственные ликвидации", own_liq <= fc.max_liquidations, own_liq, f"≤ {fc.max_liquidations}"))
    if live and prep.data.clearinghouse is not None:
        liq_dist = current_liq_distance(prep.data.clearinghouse)
    else:
        eq = prep.total_curve.av_at(t_asof)
        liq_dist = approx_liq_distance(positions_at(prep.actions, t_asof), eq, market)
    filters.append(
        _f("далеко от ликвидации", liq_dist >= fc.min_liq_distance, liq_dist, f"≥ {fc.min_liq_distance:.0%}")
    )

    # --- scale and style ---
    equity = prep.total_curve.av_at(t_asof)
    filters.append(
        _f(
            "equity",
            fc.equity_min_usd <= equity <= fc.equity_max_usd,
            equity,
            f"${fc.equity_min_usd:,.0f}–${fc.equity_max_usd:,.0f}",
        )
    )
    conc_med, conc_p95, time_in_mkt = concurrency_profile(prep.actions, t0, t_asof)
    filters.append(
        _f(
            "одновременные позиции",
            conc_med <= fc.max_typical_concurrent,
            conc_med,
            f"медиана ≤ {fc.max_typical_concurrent:g}",
        )
    )
    coin_counts = Counter(t.coin for t in trips)
    main_coins = [c for c, n in coin_counts.items() if trips and n / len(trips) >= fc.coin_min_share]
    filters.append(
        _f("монет", fc.coins_min <= len(main_coins) <= fc.coins_max, len(main_coins), f"{fc.coins_min}–{fc.coins_max}")
    )

    # --- who is it ---
    role = (prep.data.role or {}).get("role")
    filters.append(_f("не vault", role in ("user", "subAccount"), role or "не получено", "userRole = user/subAccount"))
    maker_share, fills_per_day = det.maker_stats(prep.perp_fills, t0, t_asof)
    is_mm = maker_share >= fc.market_maker.maker_share and fills_per_day >= fc.market_maker.fills_per_day
    filters.append(
        _f(
            "не маркет-мейкер",
            not is_mm,
            f"мейкер {maker_share:.0%}, {fills_per_day:.0f} филл/день",
            "мейкер ≥ 80% и ≥ 100 филл/день",
        )
    )
    filters.append(
        _f(
            "не ликвидационный бот",
            liq_counter < fc.liq_bot.counterparty_share,
            liq_counter,
            f"< {fc.liq_bot.counterparty_share:.0%} филлов",
        )
    )
    opp = det.opposite_positions_share(prep.actions, t0, t_asof, fc.hedge.opposite_ratio)
    fund_share = det.funding_pnl_share(trips)
    spot_ratio = (
        det.spot_hedge_ratio(prep.data.clearinghouse, prep.data.spot_state, market.spot_prices) if live else 0.0
    )
    hedge_ok = (
        opp < fc.hedge.opposite_time_share
        and fund_share < fc.hedge.funding_pnl_share
        and spot_ratio < fc.hedge.spot_vs_perp_share
    )
    filters.append(
        _f(
            "не хедж/delta-neutral",
            hedge_ok,
            f"противоположные {opp:.0%}, funding {fund_share:.0%}, спот {spot_ratio:.0%}",
            "одна нога не копируется",
        )
    )

    # --- metrics for the score and the report ---
    lev = effective_leverage_samples(prep.actions, prep.total_curve, t0, t_asof)
    maes = np.array([t.mae for t in trips if t.mae is not None], dtype=float)
    notionals = np.array([t.max_notional for t in trips], dtype=float)
    first_notionals = np.array([t.first_notional for t in trips], dtype=float)
    long_share = float(np.mean([t.direction > 0 for t in trips])) if trips else 0.0
    wins = [p for p in pnls if p > 0]
    metrics.update(
        {
            "trades": float(len(trips)),
            "trades_per_week": len(trips) / (fc.lookback_days / 7),
            "avg_hold_min": avg_hold / MIN,
            "median_hold_min": float(np.median(holds)) / MIN if len(holds) else 0.0,
            "mdd": mdd,
            "profitable_months": float(n_prof),
            "top3_share": top3_share,
            "pnl_90d": total,
            "return_90d": float(np.prod(1 + daily) - 1) if len(daily) else 0.0,
            "sortino": sortino(daily),
            "sharpe_daily": sharpe(daily),
            "profit_factor": profit_factor(pnls),
            "win_rate": len(wins) / len(pnls) if pnls else 0.0,
            "weeks_positive": float(np.mean(weekly > 0)) if len(weekly) else 0.0,
            "weekly_mean": float(np.mean(weekly)) if len(weekly) else 0.0,
            "weekly_std": float(np.std(weekly, ddof=1)) if len(weekly) > 1 else 0.0,
            "equity": equity,
            "concurrency_median": conc_med,
            "concurrency_p95": conc_p95,
            "time_in_market": time_in_mkt,
            "leverage_median": float(np.median(lev)) if len(lev) else 0.0,
            "leverage_p90": float(np.quantile(lev, 0.9)) if len(lev) else 0.0,
            "trip_notional_median": float(np.median(notionals)) if len(notionals) else 0.0,
            "trip_notional_p95": float(np.quantile(notionals, 0.95)) if len(notionals) else 0.0,
            "entry_notional_median": float(np.median(first_notionals)) if len(first_notionals) else 0.0,
            "mae_p95": float(np.quantile(maes, 0.95)) if len(maes) else float("nan"),
            "mae_median": float(np.median(maes)) if len(maes) else float("nan"),
            "liq_distance": liq_dist,
            "maker_share": maker_share,
            "fills_per_day": fills_per_day,
            "small_add_share": det.small_add_share(trips),
            "martingale_share": mg.trip_share,
            "funding_share": fund_share,
            "opposite_share": opp,
            "weekly_spike_z": det.weekly_spike_z(weekly),
            "account_age_days": (t_asof - curve.start) / DAY if not curve.empty else 0.0,
            "long_share": long_share,
            "limit_entry_share": float(np.mean([t.maker_open for t in trips])) if trips else 0.0,
        }
    )
    style = {
        "coins": [c for c, _ in coin_counts.most_common()],
        "coin_shares": {c: n / len(trips) for c, n in coin_counts.most_common()} if trips else {},
        "main_coins": main_coins,
        "current_leverage": _current_leverage(prep.data.clearinghouse) if live else {},
    }
    return WalletEval(prep.address, t_asof, filters, metrics, style, daily, weekly)


def _current_leverage(clearinghouse: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if isinstance(clearinghouse, dict):
        for ap in clearinghouse.get("assetPositions") or []:
            p = ap.get("position") or {}
            lev = p.get("leverage") or {}
            if p.get("coin") and lev.get("value"):
                out[str(p["coin"])] = int(lev["value"])
    return out


def stage1_pass(curve: EquityCurve, total: EquityCurve, t_asof: int, cfg: Config) -> bool:
    """Cheap portfolio-only screen with relaxed thresholds (the portfolio history is coarser than fills)."""
    fc, relax = cfg.filters, cfg.discovery.stage1_relax
    t0 = t_asof - fc.lookback_days * DAY
    if not curve.covers(t0):
        return False
    eq = total.av_at(t_asof)
    if not (fc.equity_min_usd / relax <= eq <= fc.equity_max_usd * relax):
        return False
    if curve.max_drawdown(t0, t_asof) >= min(0.95, fc.max_drawdown * relax):
        return False
    months = block_pnls(curve, t_asof, 30, fc.months_window)
    return sum(1 for m in months if math.isfinite(m) and m > 0) >= fc.min_profitable_months - 1


# ----------------------------------------------------------------------------------------------------------
# Cross-sectional passes: skill (DSR, bootstrap q-values), clusters, final score
# ----------------------------------------------------------------------------------------------------------


def apply_skill(evals: list[WalletEval], cfg: Config, n_trials: int | None = None) -> None:
    """Deflated Sharpe against the best of N unskilled tries.

    The trials are the wallets we could have picked: those passing every filter that does NOT look at results
    (style, behaviour, data). Choosing among them by drawdown, profitable months or score is the multiple testing
    that the deflation corrects for. N counts linked wallets once."""
    usable = [e for e in evals if len(e.daily) >= 30]
    if not usable:
        return
    trials = [e for e in usable if all(f.passed for f in e.filters if f.kind != "perf")]
    if len(trials) < 2:
        trials = usable
    srs = np.array([sharpe(e.daily) for e in trials])
    sr_var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    clusters_seen = {e.cluster or e.address for e in trials}
    n = n_trials or max(1, len(clusters_seen))
    for e in usable:
        e.dsr = deflated_sharpe(e.daily, sr_var, n)
        seed = int(e.address[2:10], 16) if e.address.startswith("0x") else 0  # stable across runs
        e.pvalue = bootstrap_pvalue(e.daily, cfg.score.bootstrap_samples, cfg.score.bootstrap_block_days, seed=seed)
    for e, q in zip(usable, benjamini_hochberg([e.pvalue for e in usable]), strict=True):
        e.qvalue = q


def compute_score(e: WalletEval, cfg: Config) -> float:
    w, pen, m = cfg.score.weights, cfg.score.penalties, e.metrics
    weekly_ratio = m["weekly_mean"] / m["weekly_std"] if m["weekly_std"] > 0 else 0.0
    stability = float(
        np.mean(
            [
                m["weeks_positive"],
                clip01(weekly_ratio),
                clip01(m["sortino"] / 5.0),
                clip01((m["profit_factor"] - 1.0) / 2.0),
            ]
        )
    )
    dd = clip01(1.0 - (m["mdd"] if math.isfinite(m["mdd"]) else 1.0) / cfg.filters.max_drawdown)
    copy = clip01(e.copyability or 0.0)
    parts = {
        "skill": w.skill * clip01(e.dsr),
        "stability": w.stability * stability,
        "copyability": w.copyability * copy,
        "drawdown": w.drawdown * dd,
        "pen_small_adds": -pen.small_adds * clip01(m["small_add_share"] * 2),
        "pen_weekly_spike": -pen.weekly_spike * (1.0 if m["weekly_spike_z"] > cfg.score.weekly_spike_z else 0.0),
        "pen_short_history": -pen.short_history
        * (1.0 if m["account_age_days"] < cfg.score.short_history_days else 0.0),
    }
    e.score_parts = parts
    e.score = max(0.0, min(100.0, 100.0 * sum(parts.values())))
    return e.score


def dedupe_clusters(evals: list[WalletEval], mapping: dict[str, str]) -> None:
    """Linked wallets are one trader: only the best-scored wallet of a cluster stays eligible for recommendation."""
    groups: dict[str, list[WalletEval]] = {}
    for e in evals:
        e.cluster = mapping.get(e.address, e.address)
        groups.setdefault(e.cluster, []).append(e)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda x: x.score, reverse=True)
        best = members[0]
        best.linked = [x.address for x in members[1:]]
        for x in members[1:]:
            x.linked = [best.address]
            x.filters.append(
                FilterResult(
                    "не дубль связанного кошелька", False, f"связан с {best.address}", "один трейдер = один кошелёк"
                )
            )

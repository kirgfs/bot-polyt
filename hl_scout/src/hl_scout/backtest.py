"""Walk-forward backtest of MY copy of a wallet through the copy bot.

For every fold: settings of the copy bot and the thresholds of the ПРЕКРАТИТЬ rules are chosen on the training
window only; the next test window is then simulated with them, including the rules (with my reaction delay).
Out-of-sample daily returns of all test windows feed the Monte Carlo forecast. Nothing from a test window is
used to choose anything that is applied to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from hl_scout.config import Config, CopyBotSpec, ProfileLimits
from hl_scout.market import MarketData
from hl_scout.montecarlo import McResult, combine_daily, simulate_capital
from hl_scout.replicability import (
    Replicability,
    TrainStats,
    assess,
    my_liq_distance,
    safe_leverages,
    stop_in_vain,
    train_stats,
)
from hl_scout.rules import (
    Thresholds,
    apply_stop,
    copy_dd_events,
    make_baseline,
    stopped_equity_series,
    wallet_signals,
)
from hl_scout.scoring import Prepared
from hl_scout.sim import CopySettings, CopySimulator, SimEnv, SimResult
from hl_scout.util import DAY, MIN, floor_sig

PROFILES = ("conservative", "medium", "aggressive")
PROFILE_RU = {"conservative": "консервативный", "medium": "средний", "aggressive": "агрессивный"}
MIN_COPIED_ENTRIES = 5  # a training window with fewer copied entries says nothing about the settings


@dataclass(frozen=True)
class Fold:
    k: int
    train0: int
    test0: int
    test1: int


def make_folds(data_start: int, now: int, cfg: Config) -> list[Fold]:
    """Test windows end at `now` and slide back by `step_days`; each needs the filter lookback before it."""
    bt = cfg.backtest
    need = max(cfg.filters.lookback_days, bt.train_days) * DAY
    out: list[tuple[int, int, int]] = []
    test1 = now
    while True:
        test0 = test1 - bt.test_days * DAY
        if test0 - need < data_start:
            break
        out.append((test0 - bt.train_days * DAY, test0, test1))
        test1 -= bt.step_days * DAY
    out.reverse()
    return [Fold(k, a, b, c) for k, (a, b, c) in enumerate(out)]


@dataclass
class Candidate:
    settings: CopySettings
    res: SimResult
    daily: np.ndarray
    mc: McResult
    lost: float
    vain: float | None
    copied_entries: int


@dataclass
class Choice:
    profile: str
    cand: Candidate | None
    thresholds: Thresholds
    pause: bool
    why: str = ""


@dataclass
class TestOutcome:
    profile: str
    fold: Fold
    settings: CopySettings | None
    start: float
    end: float
    traded: bool
    daily: np.ndarray
    stop_reason: str | None
    stop_t: int | None
    warnings: int
    lost: float
    ideal_pnl: float
    liquidated: bool
    why: str = ""

    @property
    def pnl(self) -> float:
        return self.end - self.start


@dataclass
class ProfileSummary:
    profile: str
    settings: CopySettings | None
    thresholds: Thresholds | None
    pause: bool
    train_mc: McResult | None
    windows: int
    profitable: int
    oos_daily: np.ndarray
    mc: McResult | None
    replicability: Replicability | None
    reject: list[str] = field(default_factory=list)

    @property
    def profitable_share(self) -> float:
        return self.profitable / self.windows if self.windows else 0.0

    @property
    def eligible(self) -> bool:
        return self.settings is not None and not self.reject


@dataclass
class WalletBacktest:
    address: str
    alloc: float
    folds: list[Fold]
    tests: list[TestOutcome]
    profiles: dict[str, ProfileSummary]
    stats_now: TrainStats | None
    copyability: float
    recommended: str | None
    risk: str
    notes: list[str] = field(default_factory=list)

    def tests_for(self, profile: str) -> list[TestOutcome]:
        return [t for t in self.tests if t.profile == profile]


def risk_level(mc: McResult | None) -> str:
    if mc is None:
        return "высокий"
    if mc.p_loss < 0.05 and mc.p_ruin < 0.01:
        return "низкий"
    if mc.p_loss < 0.15 and mc.p_ruin < 0.05:
        return "средний"
    return "высокий"


class Backtester:
    def __init__(self, cfg: Config, market: MarketData, copybot: CopyBotSpec, *, delay_s: float | None = None):
        self.cfg = cfg
        self.market = market
        self.copybot = copybot
        self.env = SimEnv.from_config(cfg, market, copybot.semantics, copybot.bot.fee_bps, delay_s)
        self.sim = CopySimulator(market, self.env)
        self.ideal = CopySimulator(market, self.env.as_ideal())
        self.th0 = Thresholds.from_config(cfg.rules, cfg.filters)
        self.reaction_ms = int(cfg.copying.human_reaction_min * MIN)
        self.exit_cost = (cfg.costs.taker_fee_bps + copybot.bot.fee_bps) / 1e4 + 0.001

    # ---------------------------------------------------------------------------------------------------
    # settings space
    # ---------------------------------------------------------------------------------------------------
    def grid(self, ts: TrainStats, alloc: float) -> list[CopySettings]:
        cfg, g = self.cfg, self.cfg.backtest.grid
        if ts.trip_notional_median <= 0 or ts.n_trips == 0:
            return []
        scale = alloc / cfg.deposit.total_usd
        min_order = cfg.copying.min_order_usd
        targets = sorted({round(max(min_order * 1.2, t * scale), 2) for t in g.target_position_usd})
        levs = safe_leverages(ts, g.leverage, cfg.recommend.liq_safety)
        balance_sl = round(alloc - cfg.project.loss_ceiling_usd * scale, 2)
        side_variants = [(True, True)]
        if ts.long_pnl < 0 < ts.short_pnl and -ts.long_pnl > 0.2 * ts.short_pnl:
            side_variants.append((False, True))
        if ts.short_pnl < 0 < ts.long_pnl and -ts.short_pnl > 0.2 * ts.long_pnl:
            side_variants.append((True, False))
        buy_times = g.buy_times if ts.increases_per_trip >= 0.3 else [0]
        out: list[CopySettings] = []
        for target in targets:
            ratio = floor_sig(target / ts.trip_notional_median, 2)
            if ratio <= 0:
                continue
            for lev in levs:
                # the wallet's usual number of simultaneous positions must fit: otherwise the bot would skip
                # its second/third position — the recommendation is a smaller size instead [2.5]
                margin = target / lev
                max_tokens = max(1, math.ceil(ts.concurrency_p95 - 1e-9))
                if max_tokens * margin > cfg.recommend.max_margin_share * alloc + 1e-9:
                    continue
                max_trade = round(g.max_trade_mult * target, 2)
                for bt in buy_times:
                    token_size = round(max(target, max_trade * (bt if bt else 3)), 2)
                    for small in g.small_size:
                        for sl in g.price_sl:
                            sl_pct = None
                            if sl == "mae":
                                sl_pct = cfg.backtest.sl_mae_mult * ts.mae_p95
                                if not (math.isfinite(sl_pct) and 0 < sl_pct < 0.9 * my_liq_distance(lev, ts.mm_rate)):
                                    continue
                            for copy_long, copy_short in side_variants:
                                sides = "" if copy_long and copy_short else ("_noLONG" if not copy_long else "_noSHORT")
                                out.append(
                                    CopySettings(
                                        alloc_usd=alloc,
                                        copy_ratio=ratio,
                                        leverage=lev,
                                        min_trade_usd=min_order,
                                        max_trade_usd=max_trade,
                                        buy_times=bt,
                                        small_size=small,
                                        max_tokens=max_tokens,
                                        max_token_size_usd=token_size,
                                        max_token_margin_usd=round(token_size / lev, 2),
                                        max_total_margin_usd=round(cfg.recommend.max_margin_share * alloc, 2),
                                        price_sl_pct=sl_pct,
                                        balance_sl_usd=balance_sl,
                                        copy_long=copy_long,
                                        copy_short=copy_short,
                                        target_usd=target,
                                        label=_label(target, lev, bt, small, sl_pct, sides),
                                    )
                                )
        return out

    # ---------------------------------------------------------------------------------------------------
    # training window
    # ---------------------------------------------------------------------------------------------------
    def _mc(self, daily: np.ndarray, s: CopySettings, paths: int, seed: int) -> McResult:
        mc, cfg = self.cfg.montecarlo, self.cfg
        scale = s.alloc_usd / cfg.deposit.total_usd
        return simulate_capital(
            daily,
            s.alloc_usd,
            cfg.goal.horizon_days,
            paths,
            mc.block_days,
            seed,
            stop_level=s.balance_sl_usd,
            ruin_usd=mc.ruin_usd * scale,
            levels=tuple(lv * scale for lv in cfg.goal.levels_usd),
            loss_threshold=mc.loss_threshold,
        )

    def evaluate_train(self, prep: Prepared, grid: list[CopySettings], t0: int, t1: int) -> list[Candidate]:
        n_days = int((t1 - t0) // DAY)
        cands: list[Candidate] = []
        cache: dict[tuple, Candidate] = {}
        for s in grid:
            # "Your Copy Size < $10 = Buy" changes nothing when no copy is below the minimum: reuse the Skip run
            twin = cache.get(_key_without_small(s))
            if s.small_size == "buy" and twin is not None and twin.res.lost_action_share() == 0:
                cands.append(replace(twin, settings=s))
                continue
            res = self.sim.run(prep.actions, s, t0, t1)
            daily = res.daily_returns(t0, n_days)
            mc = self._mc(daily, s, self.cfg.backtest.tune_mc_paths, seed=self.cfg.montecarlo.seed)
            o = res.outcomes
            copied = sum(o[f"{k}_{v}"] for k in ("open", "late_entry") for v in ("copied", "bumped"))
            cand = Candidate(s, res, daily, mc, res.lost_action_share(), stop_in_vain(res, prep)[1], copied)
            if s.small_size == "skip":
                cache[_key_without_small(s)] = cand
            cands.append(cand)
        return cands

    def choose(self, cands: list[Candidate], lim: ProfileLimits) -> tuple[Candidate | None, str]:
        rc = self.cfg.recommend
        ok = [
            c
            for c in cands
            if c.settings.leverage <= lim.max_leverage
            and c.settings.max_tokens * c.settings.target_usd <= lim.max_exposure * c.settings.alloc_usd + 1e-9
            and c.copied_entries >= MIN_COPIED_ENTRIES
            and c.lost <= rc.max_lost_actions
            and (c.vain is None or c.vain <= rc.max_stop_in_vain)
            and c.mc.p_ruin <= lim.max_p_ruin
            and c.mc.p_loss <= lim.max_p_loss
            and c.mc.median > c.settings.alloc_usd
        ]
        if not ok:
            if not cands:
                return None, "нет безопасных настроек (плечо/маржа)"
            if not any(c.copied_entries >= MIN_COPIED_ENTRIES for c in cands):
                return None, "на обучающем окне почти нечего копировать"
            return None, "ни одна настройка не даёт рост при ограничениях риска профиля"
        best = max(ok, key=lambda c: (round(c.mc.median, 4), c.mc.p5, -c.mc.p_loss))
        return best, ""

    def tune_rules(self, prep: Prepared, cand: Candidate, t0: int, t1: int) -> tuple[Thresholds, bool]:
        """Rule thresholds that would have served best on the training window (ties → config defaults)."""
        r = self.cfg.rules
        base = make_baseline(prep, t0, t1, self.th0.hedge_ratio)
        sig = wallet_signals(prep, self.market, base, self.th0, t0, t1)
        best_key, best_th = None, self.th0
        for x in r.copy_dd_grid:
            for k in r.wallet_dd7_grid:
                for n in r.consecutive_losses_grid:
                    th = self.th0.tuned(x, k, n)
                    st = apply_stop(
                        cand.res, sig.events(th) + copy_dd_events(cand.res, th), self.reaction_ms, self.exit_cost
                    )
                    dist = abs(x - r.copy_dd_stop) + abs(k - r.wallet_dd7_mult) + abs(n - r.consecutive_losses) / 10
                    key = (round(st.end, 4), -dist)
                    if best_key is None or key > best_key:
                        best_key, best_th = key, th
        pause = False
        if r.regime.enabled and self.market.regime is not None:
            spans = self.market.regime.extreme_intervals(t0, t1)
            pnl_extreme = sum(cand.res.equity_at(b) - cand.res.equity_at(a) for a, b in spans)
            pause = bool(spans) and pnl_extreme < 0
        return best_th, pause

    # ---------------------------------------------------------------------------------------------------
    # test window
    # ---------------------------------------------------------------------------------------------------
    def test(self, prep: Prepared, choice: Choice, fold: Fold, alloc: float) -> TestOutcome:
        n_days = int((fold.test1 - fold.test0) // DAY)
        if choice.cand is None:
            return TestOutcome(
                choice.profile,
                fold,
                None,
                alloc,
                alloc,
                False,
                np.zeros(n_days),
                None,
                None,
                0,
                0.0,
                0.0,
                False,
                choice.why,
            )
        s = choice.cand.settings
        pause = (
            self.market.regime.extreme_intervals(fold.test0, fold.test1) if choice.pause and self.market.regime else []
        )
        res = self.sim.run(prep.actions, s, fold.test0, fold.test1, pause)
        base = make_baseline(prep, fold.train0, fold.test0, self.th0.hedge_ratio)
        sig = wallet_signals(prep, self.market, base, choice.thresholds, fold.test0, fold.test1)
        events = sig.events(choice.thresholds) + copy_dd_events(res, choice.thresholds)
        st = apply_stop(res, events, self.reaction_ms, self.exit_cost)
        edges = fold.test0 + np.arange(n_days + 1, dtype=np.int64) * DAY
        eq = stopped_equity_series(res, st, edges)
        daily = np.where(eq[:-1] > 0, np.diff(eq) / np.maximum(eq[:-1], 1e-9), 0.0)
        ideal = self.ideal.run(prep.actions, s, fold.test0, fold.test1)
        o = res.outcomes
        traded = sum(o[f"{k}_{v}"] for k in ("open", "late_entry") for v in ("copied", "bumped")) > 0
        warns = sum(1 for e in events if e.level != "ПРЕКРАТИТЬ")
        return TestOutcome(
            choice.profile,
            fold,
            s,
            alloc,
            st.end,
            traded,
            daily,
            st.reason,
            st.t_signal,
            warns,
            res.lost_action_share(),
            ideal.pnl,
            res.liquidated,
        )

    # ---------------------------------------------------------------------------------------------------
    # one wallet
    # ---------------------------------------------------------------------------------------------------
    def choose_all(
        self, prep: Prepared, t0: int, t1: int, alloc: float
    ) -> tuple[TrainStats, list[Candidate], dict[str, Choice]]:
        ts = train_stats(prep, self.market, t0, t1)
        cands = self.evaluate_train(prep, self.grid(ts, alloc), t0, t1)
        choices: dict[str, Choice] = {}
        for name in PROFILES:
            cand, why = self.choose(cands, self.cfg.backtest.profiles[name])
            if cand is None:
                choices[name] = Choice(name, None, self.th0, False, why)
                continue
            th, pause = self.tune_rules(prep, cand, t0, t1)
            choices[name] = Choice(name, cand, th, pause)
        return ts, cands, choices

    def run_fold(self, prep: Prepared, fold: Fold, alloc: float) -> list[TestOutcome]:
        _, _, choices = self.choose_all(prep, fold.train0, fold.test0, alloc)
        return [self.test(prep, choices[name], fold, alloc) for name in PROFILES]

    def run_wallet(self, prep: Prepared, now: int, alloc: float | None = None) -> WalletBacktest:
        cfg = self.cfg
        alloc = alloc or cfg.deposit.total_usd
        data_start = max(prep.curve.start, prep.earliest_fill or now)
        folds = make_folds(data_start, now, cfg)
        tests: list[TestOutcome] = []
        for fold in folds:
            tests.extend(self.run_fold(prep, fold, alloc))
        t0 = now - cfg.backtest.train_days * DAY
        ts_now, _, choices = self.choose_all(prep, t0, now, alloc)
        profiles: dict[str, ProfileSummary] = {}
        notes: list[str] = []
        for name in PROFILES:
            ch = choices[name]
            outs = [t for t in tests if t.profile == name]
            traded = [t for t in outs if t.traded]
            oos = np.concatenate([t.daily for t in outs]) if outs else np.zeros(0)
            mc = (
                self._mc(oos, ch.cand.settings, cfg.montecarlo.paths, cfg.montecarlo.seed)
                if ch.cand and len(oos)
                else None
            )
            rep = (
                assess(prep, self.market, self.sim, ch.cand.settings, ts_now, t0, now, cfg, ch.cand.res)
                if ch.cand
                else None
            )
            summary = ProfileSummary(
                profile=name,
                settings=ch.cand.settings if ch.cand else None,
                thresholds=ch.thresholds if ch.cand else None,
                pause=ch.pause,
                train_mc=ch.cand.mc if ch.cand else None,
                windows=len(traded),
                profitable=sum(1 for t in traded if t.pnl > 0),
                oos_daily=oos,
                mc=mc,
                replicability=rep,
            )
            rc = cfg.recommend
            if ch.cand is None:
                summary.reject.append(ch.why or "нет настроек")
            if summary.windows < rc.min_test_windows:
                summary.reject.append(f"тестовых окон с копированием {summary.windows} < {rc.min_test_windows}")
            elif summary.profitable_share < rc.min_profitable_test_share:
                summary.reject.append(
                    f"прибыльных тестовых окон {summary.profitable_share:.0%} < {rc.min_profitable_test_share:.0%}"
                )
            if mc is not None and mc.p_ruin >= rc.max_p_ruin:
                summary.reject.append(f"P(обнуление) {mc.p_ruin:.1%} ≥ {rc.max_p_ruin:.0%}")
            if rep is not None and not rep.passed:
                summary.reject.extend(rep.reasons)
            profiles[name] = summary
        recommended = next((n for n in PROFILES if profiles[n].eligible), None)
        copyability = self._copyability(prep, choices, t0, now)
        if not folds:
            notes.append("истории мало для walk-forward: нет ни одного тестового окна")
        rec_mc = profiles[recommended].mc if recommended else None
        return WalletBacktest(
            prep.address, alloc, folds, tests, profiles, ts_now, copyability, recommended, risk_level(rec_mc), notes
        )

    def _copyability(self, prep: Prepared, choices: dict[str, Choice], t0: int, t1: int) -> float:
        """My copy's PnL / an ideal copy's PnL (no delay, costs or $10 minimum) at the same ratio, last window."""
        ch = next((choices[n] for n in ("medium", "conservative", "aggressive") if choices[n].cand), None)
        if ch is None or ch.cand is None:
            return 0.0
        ideal = self.ideal.run(prep.actions, ch.cand.settings, t0, t1)
        if ideal.pnl <= 0:
            return 0.0
        return max(0.0, min(1.0, ch.cand.res.pnl / ideal.pnl))

    def default_copyability(self, prep: Prepared, t0: int, t1: int, alloc: float | None = None) -> float:
        """Quick copyability for ranking before the full walk-forward: medium-sized target, safest leverage."""
        alloc = alloc or self.cfg.deposit.total_usd
        ts = train_stats(prep, self.market, t0, t1)
        grid = [
            s
            for s in self.grid(ts, alloc)
            if s.small_size == "skip" and s.price_sl_pct is None and s.buy_times == 0 and s.copy_long and s.copy_short
        ]
        if not grid:
            return 0.0
        target = sorted({s.target_usd for s in grid})[len({s.target_usd for s in grid}) // 2]
        s = min((x for x in grid if x.target_usd == target), key=lambda x: x.leverage)
        real = self.sim.run(prep.actions, s, t0, t1)
        ideal = self.ideal.run(prep.actions, s, t0, t1)
        if ideal.pnl <= 0:
            return 0.0
        return max(0.0, min(1.0, real.pnl / ideal.pnl))


def _label(target: float, lev: int, buys: int, small: str, sl_pct: float | None, sides: str) -> str:
    sl = "-" if sl_pct is None else f"{sl_pct:.1%}"
    return f"${target:g}·{lev}x·buys{buys or '∞'}·{small}·SL{sl}{sides}"


def _key_without_small(s: CopySettings) -> tuple:
    return (
        s.copy_ratio,
        s.leverage,
        s.max_trade_usd,
        s.buy_times,
        s.max_tokens,
        s.max_token_size_usd,
        s.price_sl_pct,
        s.copy_long,
        s.copy_short,
    )


def split_comparison(
    a: WalletBacktest, b: WalletBacktest, single: WalletBacktest, cfg: Config, profile: str
) -> dict[str, McResult | None]:
    """1 wallet × $50 vs 2 wallets × $25 (each re-checked for the $10 minimum and concurrency at $25)."""
    out: dict[str, McResult | None] = {"single": single.profiles[profile].mc}
    pa, pb = a.profiles[profile], b.profiles[profile]
    if pa.settings is None or pb.settings is None or not len(pa.oos_daily) or not len(pb.oos_daily):
        out["split"] = None
        return out
    daily = combine_daily(pa.oos_daily, pb.oos_daily)
    mc = cfg.montecarlo
    out["split"] = simulate_capital(
        daily,
        cfg.deposit.total_usd,
        cfg.goal.horizon_days,
        mc.paths,
        mc.block_days,
        mc.seed,
        stop_level=cfg.deposit.total_usd - cfg.project.loss_ceiling_usd,
        ruin_usd=mc.ruin_usd,
        levels=tuple(cfg.goal.levels_usd),
        loss_threshold=mc.loss_threshold,
    )
    return out

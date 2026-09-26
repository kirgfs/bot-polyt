"""End-to-end analysis over the cache: filters → skill → score → walk-forward → Monte Carlo → process check."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hl_scout.backtest import PROFILES, Backtester, Fold, TestOutcome, WalletBacktest, make_folds, split_comparison
from hl_scout.config import Config, CopyBotSpec
from hl_scout.detectors import Link, clusters, find_links
from hl_scout.discovery import deep_addresses, load_market, load_wallet, traded_coins
from hl_scout.log import get_logger
from hl_scout.market import MarketData
from hl_scout.montecarlo import McResult, simulate_capital
from hl_scout.scoring import Prepared, WalletEval, apply_skill, compute_score, dedupe_clusters, evaluate, prepare
from hl_scout.store import Store
from hl_scout.util import DAY

log = get_logger(__name__)


@dataclass
class ProcessResult:
    """Walk-forward of the whole scout: at each fold the wallet is picked with data available at that time."""

    picks: list[tuple[Fold, str | None]]
    outcomes: dict[str, list[TestOutcome]]
    mc: dict[str, McResult | None]
    n_wallets: int = 0
    control: bool = False  # True: only the random control sample (no selection on past profit) was used

    def profitable_share(self, profile: str) -> float:
        traded = [o for o in self.outcomes[profile] if o.traded]
        return sum(1 for o in traded if o.pnl > 0) / len(traded) if traded else 0.0


@dataclass
class ScoutRun:
    now: int
    cfg: Config
    copybot: CopyBotSpec
    evals: list[WalletEval]
    ranked: list[WalletEval]
    backtests: dict[str, WalletBacktest]
    split: dict[str, Any] | None
    process: ProcessResult | None
    links: list[Link]
    funnel: dict[str, int]
    preps: dict[str, Prepared] = field(default_factory=dict)
    forced: list[WalletEval] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def recommendable(self) -> list[WalletEval]:
        """Hard filters passed, skill gate passed, at least one copy-bot profile eligible."""
        out = []
        for e in self.ranked:
            bt = self.backtests.get(e.address)
            if bt and bt.recommended and e.dsr >= self.cfg.recommend.min_dsr:
                out.append(e)
        return out[: self.cfg.deposit.max_wallets]


def analyze(
    cfg: Config,
    store: Store,
    copybot: CopyBotSpec,
    now: int,
    *,
    top_n: int = 10,
    only: list[str] | None = None,
    process: bool = True,
    delay_s: float | None = None,
    force: list[str] | None = None,
) -> ScoutRun:
    addrs = only or deep_addresses(store)
    wallets = [w for w in (load_wallet(store, a) for a in addrs) if w.portfolio is not None]
    market = load_market(store, traded_coins(wallets, cfg.universe.allow_hip3), cfg)
    control = {a for a, src in store.addresses().items() if "control" in src}
    return analyze_wallets(
        cfg,
        wallets,
        market,
        copybot,
        now,
        top_n=top_n,
        process=process,
        delay_s=delay_s,
        force=force,
        control=control or None,
    )


def analyze_wallets(
    cfg: Config,
    wallets: list,
    market: MarketData,
    copybot: CopyBotSpec,
    now: int,
    *,
    top_n: int = 10,
    process: bool = True,
    delay_s: float | None = None,
    force: list[str] | None = None,
    control: set[str] | None = None,
) -> ScoutRun:
    """`force`: addresses to analyse in full even if they fail the filters or are outside the top (/check).
    `control`: the random control sample; the process check runs only on it (None → on all wallets)."""
    preps = {w.address: prepare(w, market, cfg, now) for w in wallets}
    evals = [evaluate(p, market, cfg, now, live=True) for p in preps.values()]
    cl = cfg.filters.cluster
    masters = {
        a: str(((p.data.role or {}).get("data") or {}).get("master", "")).lower()
        for a, p in preps.items()
        if (p.data.role or {}).get("role") == "subAccount"
    }
    links = find_links(
        {a: p.actions for a, p in preps.items()},
        {a: p.data.ledger for a, p in preps.items()},
        masters,
        sync_window_s=cl.sync_window_s,
        sync_share=cl.sync_share,
        min_matches=cl.min_matches,
        max_counterparty_degree=cl.max_counterparty_degree,
    )
    mapping = clusters(list(preps), links)
    for e in evals:
        e.cluster = mapping.get(e.address, e.address)
    apply_skill(evals, cfg)
    bt = Backtester(cfg, market, copybot, delay_s=delay_s)
    t0 = now - cfg.backtest.train_days * DAY
    for e in evals:
        if e.eligible:
            e.copyability = bt.default_copyability(preps[e.address], t0, now)
        compute_score(e, cfg)
    dedupe_clusters(evals, mapping)
    ranked = sorted((e for e in evals if e.eligible), key=lambda e: e.score, reverse=True)
    log.info("scored", wallets=len(evals), eligible=len(ranked))
    backtests: dict[str, WalletBacktest] = {}
    for i, e in enumerate(ranked[:top_n]):
        backtests[e.address] = bt.run_wallet(preps[e.address], now)
        e.copyability = backtests[e.address].copyability
        compute_score(e, cfg)
        log.info(
            "backtested",
            n=i + 1,
            of=min(top_n, len(ranked)),
            address=e.address,
            recommended=backtests[e.address].recommended,
        )
    ranked.sort(key=lambda e: e.score, reverse=True)
    forced = [e for e in evals if e.address in set(force or [])]
    for e in forced:
        if e.address not in backtests:
            backtests[e.address] = bt.run_wallet(preps[e.address], now)
            e.copyability = backtests[e.address].copyability
            compute_score(e, cfg)
    run = ScoutRun(now, cfg, copybot, evals, ranked, backtests, None, None, links, {}, preps, forced=forced)
    recs = run.recommendable()
    if cfg.deposit.compare_split and len(recs) >= 2:
        half = cfg.deposit.total_usd / 2
        first = backtests[recs[0].address]
        profile = first.recommended or "conservative"
        a = bt.run_wallet(preps[recs[0].address], now, alloc=half)
        b = bt.run_wallet(preps[recs[1].address], now, alloc=half)
        run.split = {
            "a": recs[0].address,
            "b": recs[1].address,
            "profile": profile,
            "a_bt": a,
            "b_bt": b,
            **split_comparison(a, b, first, cfg, profile),
        }
    if process:
        sample = {a: p for a, p in preps.items() if control is None or a in control}
        run.process = run_process(cfg, bt, sample, market, mapping, now)
        run.process.control = control is not None
    run.funnel = funnel(evals)
    return run


def run_process(
    cfg: Config, bt: Backtester, preps: dict[str, Prepared], market: MarketData, mapping: dict[str, str], now: int
) -> ProcessResult:
    folds = make_folds(now - cfg.discovery.history_days * DAY, now, cfg)
    outcomes: dict[str, list[TestOutcome]] = {p: [] for p in PROFILES}
    picks: list[tuple[Fold, str | None]] = []
    alloc = cfg.deposit.total_usd
    for fold in folds:
        evs = [evaluate(p, market, cfg, fold.test0, live=False) for p in preps.values()]
        for e in evs:
            e.cluster = mapping.get(e.address, e.address)
        apply_skill(evs, cfg)
        elig = [e for e in evs if e.eligible and e.dsr >= cfg.recommend.min_dsr]
        for e in elig:
            e.copyability = bt.default_copyability(preps[e.address], fold.train0, fold.test0)
            compute_score(e, cfg)
        if not elig:
            picks.append((fold, None))
            n_days = int((fold.test1 - fold.test0) // DAY)
            for p in PROFILES:
                outcomes[p].append(
                    TestOutcome(
                        p,
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
                        "никто не прошёл отбор на эту дату",
                    )
                )
            continue
        best = max(elig, key=lambda e: e.score)
        picks.append((fold, best.address))
        for o in bt.run_fold(preps[best.address], fold, alloc):
            outcomes[o.profile].append(o)
        log.info("process_fold", fold=fold.k, pick=best.address)
    mcs: dict[str, McResult | None] = {}
    m = cfg.montecarlo
    for p in PROFILES:
        daily = np.concatenate([o.daily for o in outcomes[p]]) if outcomes[p] else np.zeros(0)
        mcs[p] = (
            simulate_capital(
                daily,
                alloc,
                cfg.goal.horizon_days,
                m.paths,
                m.block_days,
                m.seed,
                stop_level=alloc - cfg.project.loss_ceiling_usd,
                ruin_usd=m.ruin_usd,
                levels=tuple(cfg.goal.levels_usd),
                loss_threshold=m.loss_threshold,
            )
            if len(daily)
            else None
        )
    return ProcessResult(picks, outcomes, mcs, n_wallets=len(preps))


def funnel(evals: list[WalletEval]) -> dict[str, int]:
    """How many wallets each hard filter removed (a wallet can fail several)."""
    out: dict[str, int] = {"проверено": len(evals), "прошли все фильтры": sum(1 for e in evals if e.eligible)}
    for e in evals:
        for f in e.failed:
            out[f"не прошли: {f.name}"] = out.get(f"не прошли: {f.name}", 0) + 1
    return out

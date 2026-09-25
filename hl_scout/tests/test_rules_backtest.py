"""Rules, folds without look-ahead, walk-forward on a synthetic wallet, the full pipeline and the report."""

from __future__ import annotations

import itertools
import json

import numpy as np
import pytest

from hl_scout import report
from hl_scout.backtest import PROFILES, Backtester, make_folds
from hl_scout.config import Config, CopyBotSpec, load_config, load_copybot
from hl_scout.pipeline import analyze_wallets
from hl_scout.replicability import my_liq_distance, safe_leverages, train_stats
from hl_scout.rules import STOP, WARN, Thresholds, apply_stop, copy_dd_events, make_baseline, wallet_events
from hl_scout.scoring import prepare
from hl_scout.sim import CopySettings, CopySimulator
from hl_scout.util import DAY, HOUR, MIN

from helpers import T0, act, env, flat_then, make_market
from synth import TraderSpec, make_wallet


def test_config_files_parse_and_copybot_is_unverified():
    cfg = load_config("config.yaml")
    assert cfg.deposit.total_usd == 50 and cfg.copying.delay_s == 30
    assert cfg.backtest.profiles["conservative"].max_leverage == 3
    bot = load_copybot("copybot_fields.yaml")
    assert not bot.verified and "Copy Ratio" in bot.unverified_fields
    assert [f.key for f in bot.fields][:4] == ["target_wallet", "tag", "reverse_copy", "copy_ratio"]


def test_folds_never_overlap_and_train_precedes_test():
    cfg = Config()
    now = T0 + 200 * DAY
    folds = make_folds(T0, now, cfg)
    assert folds and folds[-1].test1 == now
    for f in folds:
        assert f.train0 < f.test0 < f.test1
        assert f.test0 - T0 >= cfg.filters.lookback_days * DAY
    for a, b in itertools.pairwise(folds):
        assert a.test1 <= b.test0


def test_copy_drawdown_rule_and_reaction_delay():
    prices = [100.0] * 60 + [100 - i * 0.1 for i in range(1, 201)] + [80.0] * 300  # slow slide 100 → 80
    m = make_market({"BTC": flat_then(prices)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 500 * MIN, "BTC", -1, 10, 80, 10)]
    res = CopySimulator(m, env(m)).run(acts, CopySettings(alloc_usd=50, copy_ratio=0.2, leverage=5), T0, T0 + 9 * HOUR)
    th = Thresholds.from_config(Config().rules)
    ev = copy_dd_events(res, th)
    assert [e.level for e in ev] == [WARN, STOP]
    st = apply_stop(res, ev, reaction_ms=60 * MIN, exit_cost=0.0)
    assert st.t_exit == ev[1].t + 60 * MIN
    # STOP at −20% ($40, price 95); I switch the bot off an hour later at price ≈ 89 → $28, not $40 …
    assert st.end == pytest.approx(res.equity_at(st.t_exit))
    assert st.end == pytest.approx(28.0, abs=0.5)
    # … which is still better than riding the wallet down to 80 ($10)
    assert st.end > res.end == pytest.approx(10.0)


def test_wallet_rules_on_synthetic_martingale(world, market):
    spec = TraderSpec(
        "0x" + "3" * 40,
        skill=0.55,
        trades_per_day=2.0,
        hold_min=(60, 600),
        notional=20_000,
        equity=60_000,
        adds=2,
        add_into_loss=True,
    )
    prep = prepare(make_wallet(world, spec, seed=12), market, Config())
    t_a = world.t_end - 14 * DAY
    base = make_baseline(prep, t_a - 60 * DAY, t_a)
    ev = wallet_events(prep, market, base, Thresholds.from_config(Config().rules), t_a, world.t_end)
    assert any(e.rule == "averaging_down" and e.level == STOP for e in ev)
    assert all(t_a <= e.t < world.t_end for e in ev)


def test_safe_leverage_respects_mae(world, market, good_wallet):
    prep = prepare(good_wallet, market, Config())
    ts = train_stats(prep, market, world.t_end - 60 * DAY, world.t_end)
    levs = safe_leverages(ts, [1, 2, 3, 5, 10, 20], 1.5)
    assert levs and all(my_liq_distance(lv, ts.mm_rate) >= 1.5 * ts.mae_p95 for lv in levs)
    assert levs == sorted(levs)


def test_walk_forward_on_good_wallet(world, market, fast_cfg, good_wallet):
    prep = prepare(good_wallet, market, fast_cfg)
    bt = Backtester(fast_cfg, market, CopyBotSpec())
    wb = bt.run_wallet(prep, world.t_end)
    assert wb.folds
    for f in wb.folds:
        outs = [t for t in wb.tests if t.fold is f]
        assert {o.profile for o in outs} == set(PROFILES)
        for o in outs:
            assert len(o.daily) == int((f.test1 - f.test0) // DAY)
            if o.settings is not None:
                assert o.settings.leverage <= fast_cfg.backtest.profiles[o.profile].max_leverage
    cons = wb.profiles["conservative"]
    if cons.mc is not None:
        assert 0.0 <= cons.mc.p_ruin <= 1.0 and cons.mc.sample_days == len(cons.oos_daily)
    assert 0.0 <= wb.copyability <= 1.0


def test_pipeline_and_report_end_to_end(world, market, fast_cfg, good_wallet):
    wallets = [
        good_wallet,
        make_wallet(
            world,
            TraderSpec("0x" + "4" * 40, skill=0.6, trades_per_day=20, hold_min=(1, 8), notional=5_000, equity=20_000),
            seed=4,
        ),
        make_wallet(
            world,
            TraderSpec(
                "0x" + "6" * 40, skill=0.7, trades_per_day=0.8, hold_min=(300, 3000), notional=4_000, equity=500_000
            ),
            seed=6,
        ),
    ]
    run = analyze_wallets(
        fast_cfg, wallets, market, CopyBotSpec(), world.t_end, top_n=3, process=True, force=["0x" + "6" * 40]
    )
    by = {e.address: e for e in run.evals}
    assert not by["0x" + "4" * 40].eligible  # scalper: holds < 15 min
    assert any(f.name == "equity" and not f.passed for f in by["0x" + "6" * 40].filters)  # whale
    assert run.funnel["проверено"] == 3
    md = report.render(run)
    for section in (
        "## Коротко",
        "## Воронка отбора",
        "## Топ-10 по score",
        "## Разбор запрошенных адресов",
        "## Допущения и ограничения",
    ):
        assert section in md
    assert "Семантика полей copy-бота не подтверждена" in md
    json.dumps(report.to_json(run), default=str)
    assert run.process is not None and len(run.process.picks) == len(
        make_folds(world.t_end - 180 * DAY, world.t_end, fast_cfg)
    )


def test_score_never_uses_the_target(fast_cfg):
    # the $1000 goal is reported, never optimised: changing it must not change any chosen setting
    a = Config(goal=fast_cfg.goal.model_copy(update={"target_usd": 1000}))
    b = Config(goal=fast_cfg.goal.model_copy(update={"target_usd": 5000}))
    assert a.backtest == b.backtest and a.recommend == b.recommend
    assert np.isclose(a.goal.horizon_days, b.goal.horizon_days)


@pytest.mark.integration
async def test_live_selfcheck():  # pragma: no cover - needs network access to api.hyperliquid.xyz
    from hl_scout.cli import selfcheck

    res = await selfcheck(Config())
    assert all(r["ok"] for r in res)

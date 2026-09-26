"""Copy simulator on hand-made price paths: every number here can be checked with a calculator."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from hl_scout.config import CopySemantics
from hl_scout.sim import CopySettings, CopySimulator
from hl_scout.util import HOUR, MIN

from helpers import T0, act, env, flat_then, make_market


def step_prices(before: float, after: float, n_before: int, n_after: int) -> list[float]:
    return [before] * n_before + [after] * n_after


def settings(**kw) -> CopySettings:
    base = dict(alloc_usd=50.0, copy_ratio=0.02, leverage=2)
    base.update(kw)
    return CopySettings(**base)


def test_long_trade_pnl_without_costs():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m)).run(acts, settings(), T0, T0 + 3 * HOUR)
    # 0.02 × 10 BTC = 0.2 BTC bought at 100, sold at 110
    assert res.pnl == pytest.approx(2.0, abs=1e-9)
    assert res.outcomes["open_copied"] == 1 and res.outcomes["close_copied"] == 1
    assert res.closed_pnls == [pytest.approx(2.0)]


def test_fees_are_charged_on_both_legs():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m, fee_bps=4.5, bot_bps=10)).run(acts, settings(), T0, T0 + 3 * HOUR)
    fees = (20 + 22) * 0.00145
    assert res.fees == pytest.approx(fees)
    assert res.pnl == pytest.approx(2.0 - fees)


def test_copy_below_minimum_is_skipped_or_bumped():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    sim = CopySimulator(m, env(m))
    skipped = sim.run(acts, settings(copy_ratio=0.001), T0, T0 + 3 * HOUR)  # $1 copy
    assert skipped.outcomes["open_skipped_small"] == 1
    assert skipped.outcomes["close_no_position"] == 1
    assert skipped.pnl == 0.0
    assert skipped.lost_action_share() == 1.0
    bumped = sim.run(acts, settings(copy_ratio=0.001, small_size="buy"), T0, T0 + 3 * HOUR)
    assert bumped.outcomes["open_bumped"] == 1
    assert bumped.pnl == pytest.approx(1.0)  # $10 bought at 100 → 0.1 BTC × $10


def test_ideal_copy_ignores_minimum_and_costs():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    ideal = CopySimulator(m, env(m, fee_bps=4.5, bot_bps=10, slip=0.001, delay_s=30).as_ideal())
    res = ideal.run(acts, settings(copy_ratio=0.001), T0, T0 + 3 * HOUR)
    assert res.pnl == pytest.approx(0.1)


def test_cross_liquidation_wipes_the_account():
    prices = [100.0] * 60 + [100 - i * 0.5 for i in range(1, 41)] + [80.0] * 60
    m = make_market({"BTC": flat_then(prices)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 150 * MIN, "BTC", -1, 10, 80, 10)]
    res = CopySimulator(m, env(m)).run(acts, settings(copy_ratio=0.4, leverage=10), T0, T0 + 3 * HOUR)
    # 4 BTC ($400) on $50: equity hits maintenance margin (1.25% of notional) around price 88
    assert res.liquidated
    assert res.end == 0.0
    assert res.stopped_at is not None and res.stopped_at < T0 + 150 * MIN


def test_isolated_liquidation_loses_only_the_position_margin():
    prices = [100.0] * 60 + [100 - i * 0.5 for i in range(1, 41)] + [80.0] * 60
    m = make_market({"BTC": flat_then(prices)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 150 * MIN, "BTC", -1, 10, 80, 10)]
    sem = CopySemantics(margin_mode="isolated")
    res = CopySimulator(m, env(m, semantics=sem)).run(acts, settings(copy_ratio=0.2, leverage=10), T0, T0 + 3 * HOUR)
    assert res.liquidated
    assert res.end == pytest.approx(50 - 20)  # $200 at 10x → the $20 margin is lost, the rest of the account is not


def test_price_stop_loss_exits_at_the_level():
    prices = [100.0] * 60 + [100 - i * 0.25 for i in range(1, 41)] + [90.0] * 60
    m = make_market({"BTC": flat_then(prices)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 150 * MIN, "BTC", -1, 10, 90, 10)]
    s = settings(copy_ratio=0.2, leverage=5, price_sl_pct=0.05)  # 2 BTC = $200, margin $40
    res = CopySimulator(m, env(m, stop_slip=0.005)).run(acts, s, T0, T0 + 3 * HOUR)
    assert [s.kind for s in res.stops] == ["price_sl"]
    assert res.pnl == pytest.approx(2 * (95 * 0.995 - 100))
    assert res.outcomes["close_no_position"] == 1  # the trader's later close has nothing to close


def test_balance_stop_loss_closes_everything_and_stops_copying():
    prices = [100.0] * 60 + [100 - i * 0.5 for i in range(1, 25)] + [88.0] * 60
    m = make_market({"BTC": flat_then(prices)})
    acts = [
        act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0),
        act(T0 + 150 * MIN, "BTC", -1, 10, 88, 10),
        act(T0 + 160 * MIN, "BTC", 1, 10, 88, 0),
    ]
    res = CopySimulator(m, env(m)).run(acts, settings(copy_ratio=0.2, leverage=5, balance_sl_usd=30), T0, T0 + 4 * HOUR)
    assert res.stopped_at is not None
    assert res.end == pytest.approx(30.0)  # exits when equity touches $30 (price 90)
    assert res.outcomes["open_copied"] == 1  # nothing is copied after the stop


def test_funding_is_paid_hourly_on_my_position():
    m = make_market({"BTC": flat_then([100.0] * (12 * 60))}, funding_rate=0.0001)
    acts = [act(T0 + 5 * MIN, "BTC", 1, 50, 100, 0), act(T0 + 10 * HOUR + 30 * MIN, "BTC", -1, 50, 100, 50)]
    res = CopySimulator(m, env(m)).run(acts, settings(copy_ratio=0.02), T0, T0 + 11 * HOUR)
    # 1 BTC long × $100 × 0.0001 per hour, 10 funding hours inside the position
    assert res.funding == pytest.approx(-0.10, abs=1e-9)
    assert res.pnl == pytest.approx(-0.10, abs=1e-9)


def test_delay_moves_my_entry_price():
    prices = [100.0] * 10 + [110.0] + [110.0] * 50
    m = make_market({"BTC": flat_then(prices)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 40 * MIN, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m, delay_s=30)).run(acts, settings(copy_ratio=0.2), T0, T0 + HOUR)
    # the minute after the trader's buy goes 100 → 110; 30 s later I pay ~105, not 100
    assert 1.0 < res.pnl < 20.0 * 0.2 * 10 * 0.6


def test_reverse_copy_inverts_the_result():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m)).run(acts, settings(reverse=True), T0, T0 + 3 * HOUR)
    assert res.pnl == pytest.approx(-2.0)


def test_late_entry_when_the_first_buy_was_too_small():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [
        act(T0 + 10 * MIN, "BTC", 1, 2, 100, 0),  # 0.02 × 2 BTC = $4 → skipped
        act(T0 + 20 * MIN, "BTC", 1, 8, 100, 2),  # add: $16 → the bot opens now
        act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10),
    ]
    res = CopySimulator(m, env(m)).run(acts, settings(), T0, T0 + 3 * HOUR)
    assert res.outcomes["open_skipped_small"] == 1
    assert res.outcomes["late_entry_copied"] == 1
    assert res.pnl == pytest.approx(0.16 * 10)


def test_partial_close_below_minimum_is_skipped_and_full_close_still_executes():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [
        act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0),  # $20
        act(T0 + 70 * MIN, "BTC", -1, 1, 110, 10),  # partial: 0.02 × 1 × 110 = $2.2 → skipped
        act(T0 + 2 * HOUR, "BTC", -1, 9, 110, 9),
    ]
    res = CopySimulator(m, env(m)).run(acts, settings(), T0, T0 + 3 * HOUR)
    assert res.outcomes["reduce_skipped_small"] == 1
    assert res.outcomes["close_copied"] == 1
    assert res.pnl == pytest.approx(2.0)


def test_order_is_cut_to_free_margin():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m)).run(acts, settings(copy_ratio=0.2, leverage=2), T0, T0 + 3 * HOUR)
    # wants $200 at 2x = $100 margin, has $50 → buys $100 (1 BTC)
    assert res.pnl == pytest.approx(10.0)


def test_max_tokens_and_margin_caps():
    b = flat_then([100.0] * 180)
    m = make_market({"BTC": b, "ETH": flat_then([100.0] * 180)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 11 * MIN, "ETH", 1, 10, 100, 0)]
    res = CopySimulator(m, env(m)).run(acts, settings(max_tokens=1), T0, T0 + 3 * HOUR)
    assert res.outcomes["open_skipped_max_tokens"] == 1
    capped = CopySimulator(m, env(m)).run(
        acts, settings(copy_ratio=0.2, leverage=1, max_total_margin_usd=150), T0, T0 + 3 * HOUR
    )
    # $200 wanted at 1x but only $50 of equity → the bot uses what it has; second coin gets nothing
    assert capped.max_margin <= 50.0 + 1e-9
    assert capped.outcomes["open_skipped_limits"] == 1


def test_equity_path_and_daily_returns_are_consistent():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 3000))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m)).run(acts, settings(), T0, T0 + 2 * 24 * HOUR)
    daily = res.daily_returns(T0, 2)
    assert (1 + daily).prod() * 50 == pytest.approx(res.end)
    assert res.equity_at(T0) == pytest.approx(50.0)


def _with_equity(a, equity):
    import dataclasses

    return dataclasses.replace(a, trader_equity=equity)


def test_balance_scaled_ratio_follows_the_bot_formula_and_compounds():
    # ApexLiquid: Your Copy Size = (Target Size ÷ Target Balance) × Your Balance × Copy Ratio
    sem = CopySemantics(ratio_applies_to="balance_scaled")
    m = make_market({"BTC": flat_then(step_prices(100, 200, 60, 240))})
    acts = [
        _with_equity(act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), 2_000),  # trader: 10 BTC ($1000) on $2000
        _with_equity(act(T0 + 2 * HOUR, "BTC", -1, 10, 200, 10), 3_000),
        _with_equity(act(T0 + 3 * HOUR, "BTC", 1, 10, 200, 0), 3_000),  # same trade after the win
    ]
    res = CopySimulator(m, env(m, semantics=sem)).run(acts, settings(copy_ratio=1.0), T0, T0 + 4 * HOUR)
    # first entry: 10 / 2000 × 50 × 1 = 0.25 BTC ($25); +$25 at 200 → equity $75
    assert res.closed_pnls[0] == pytest.approx(25.0)
    # second entry: 10 / 3000 × 75 = 0.25 BTC again ($50) — my balance grew as much as theirs
    i = int(np.searchsorted(res.path_t, T0 + 3 * HOUR, side="right")) - 1
    assert res.path_notional[i] == pytest.approx(50.0)


def test_balance_scaled_without_target_balance_is_a_lost_action():
    sem = CopySemantics(ratio_applies_to="balance_scaled")
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m, semantics=sem)).run(acts, settings(copy_ratio=1.0), T0, T0 + 3 * HOUR)
    assert res.outcomes["open_no_target_balance"] == 1 and res.pnl == 0.0
    assert res.lost_action_share() == 1.0


def test_max_leverage_locks_margin_at_the_coins_maximum():
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))}, max_lev=40)
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m)).run(acts, settings(copy_ratio=0.2, leverage=0), T0, T0 + 3 * HOUR)
    # 2 BTC ($200) on $50 at max leverage 40x: margin $5, so the $50 account can hold it; +$20 at 110
    assert res.max_margin == pytest.approx(5.0)
    assert res.pnl == pytest.approx(20.0)


def test_isolated_only_coins_and_low_target_balance_are_not_entered():
    sem = CopySemantics(skip_isolated_only=True)
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    m.meta["BTC"] = dataclasses.replace(m.meta["BTC"], only_isolated=True)
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    res = CopySimulator(m, env(m, semantics=sem)).run(acts, settings(), T0, T0 + 3 * HOUR)
    assert res.outcomes["open_isolated_only"] == 1 and res.pnl == 0.0
    assert res.lost_action_share() == 1.0

    m2 = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    poor = [_with_equity(a, 1_000) for a in acts]  # the trader's balance fell below my floor of $5000
    res2 = CopySimulator(m2, env(m2)).run(poor, settings(target_min_balance_usd=5_000), T0, T0 + 3 * HOUR)
    assert res2.outcomes["open_target_low_balance"] == 1 and res2.pnl == 0.0


def test_copy_below_the_bots_minimum_is_bought_after_the_wait_at_the_later_price():
    # the 1-minute bar right after the trader's buy goes from 100 to 110
    m = make_market({"BTC": flat_then([100.0] * 10 + [110.0] * 200)})
    acts = [act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0), act(T0 + 2 * HOUR, "BTC", -1, 10, 110, 10)]
    s = settings(copy_ratio=0.001, small_size="buy", min_trade_usd=15.0)  # $1 copy → bumped to $15
    now = CopySimulator(m, env(m)).run(acts, s, T0, T0 + 3 * HOUR)
    later = CopySimulator(m, env(m, semantics=CopySemantics(small_wait_s=31))).run(acts, s, T0, T0 + 3 * HOUR)
    assert now.outcomes["open_bumped"] == 1 and later.outcomes["open_bumped"] == 1
    assert now.pnl == pytest.approx(0.15 * 10)  # 0.15 BTC at 100 → 110
    px = 100 + 10 * 31_000 / (MIN - 1)  # 31 s into the rising bar
    size = math.floor(15 / px * 1e5 + 1e-9) / 1e5  # re-rounded to the lot (szDecimals 5) at the later price
    assert later.pnl == pytest.approx(size * (110 - px))
    assert later.pnl < now.pnl


def test_partial_sell_below_the_exchange_minimum_is_skipped_when_the_bot_rule_covers_buys_only():
    sem = CopySemantics(reduce_mode="proportional", small_size_applies_to_reduce=False, reduce_min_usd=10.0)
    m = make_market({"BTC": flat_then(step_prices(100, 110, 60, 120))})
    acts = [
        act(T0 + 10 * MIN, "BTC", 1, 10, 100, 0),
        act(T0 + 70 * MIN, "BTC", -1, 3, 110, 10),  # trader sells 30%: my 0.2 BTC → 0.06 BTC = $6.6 < $10
        act(T0 + 80 * MIN, "BTC", -1, 5, 110, 7),  # sells 5/7: my 0.2 → 0.142857 BTC = $15.7 → copied
    ]
    res = CopySimulator(m, env(m, semantics=sem)).run(acts, settings(), T0, T0 + 3 * HOUR)
    assert res.outcomes["reduce_skipped_small"] == 1 and res.outcomes["reduce_copied"] == 1

"""Mini-bot engine on scripted books: quotes, fills, fail-closed pulls, limits, days, settlement."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from polybot.data.records import Source
from polybot.minibot.engine import STATE_FILE, Engine, final_payouts
from polybot.minibot.model import Phase
from polybot.minibot.selection import select
from polybot.venues.base import Side
from tests.minibot_helpers import (
    MIN,
    T0,
    Clock,
    FakePool,
    H,
    book_event,
    make_engine,
    mini_config,
    pairs,
    raw_event,
    trade_event,
)

D = Decimal
START = T0 + 5 * H
INTER = "100001"  # Yes token of "Will Inter win?" in raw_event("1000", ...)
BIDS = [("0.40", "300"), ("0.39", "500")]
ASKS = [("0.44", "300"), ("0.45", "500")]


async def quoting_engine(tmp_path: Path) -> tuple[Engine, FakePool, Clock]:
    clock = Clock()
    engine, pool, _, _ = make_engine(tmp_path, clock)
    chosen = select(pairs(raw_event("1000", START)), engine.cfg, clock.now, paper=True).chosen
    pool.set_assets(await engine.apply_selection(chosen))
    pool.feed(engine, book_event(INTER, BIDS, ASKS), clock.now)
    await engine.step()
    return engine, pool, clock


def quotes(engine: Engine, token: str = INTER) -> dict[Side, tuple[Decimal, Decimal]]:
    return {o.side: (o.price, o.size) for o in engine.venue.open_orders(token)}


async def test_quotes_inside_the_rewards_band_with_rewards_size(tmp_path: Path) -> None:
    engine, _, _ = await quoting_engine(tmp_path)
    # Microprice 0.42; half-spread 0.6 × 3.5¢ = 2.1¢ → bid floor(0.399), ask ceil(0.441).
    assert quotes(engine) == {Side.BUY: (D("0.39"), D(20)), Side.SELL: (D("0.45"), D(20))}
    slot = engine.slots[INTER]
    assert slot.phase is Phase.QUOTING
    # Other markets of the match have no book yet: fail-closed, nothing quoted.
    assert all(engine.slots[t].phase is Phase.WAITING for t in engine.slots if t != INTER)
    status = engine.status()
    assert status.markets[0].bid == "0.39" and status.markets[0].ask == "0.45"


async def test_trade_through_our_bid_fills_and_is_recorded(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    clock.now += 200 * 1_000_000  # the orders reached the book
    pool.feed(engine, book_event(INTER, BIDS, ASKS), clock.now)
    pool.feed(engine, trade_event(INTER, "0.38", "30"), clock.now)
    holding = engine.venue.holdings[INTER]
    assert holding.long == D(20) and engine.day.fills == 1
    assert abs(engine.day.volume_usd - 7.8) < 1e-9
    fills = [r for r in engine.sink.records if r.source == Source.PAPER and r.event_type == "fill"]  # type: ignore[attr-defined]
    assert json.loads(fills[0].payload)["price"] == "0.39"
    # With inventory the next quotes lean lower (skew) and the bid stays within limits.
    clock.now += 3 * 1_000_000_000
    await engine.step()
    assert quotes(engine)[Side.SELL][0] <= D("0.45")


async def test_quiet_requote_keeps_the_queue_place(tmp_path: Path) -> None:
    engine, _, clock = await quoting_engine(tmp_path)
    ids = {o.order_id for o in engine.venue.open_orders(INTER)}
    clock.now += 5 * 1_000_000_000
    await engine.step()
    assert {o.order_id for o in engine.venue.open_orders(INTER)} == ids


async def test_pull_before_kickoff_is_final(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    clock.now = START - 74 * MIN
    await engine.step()
    assert engine.slots[INTER].phase is Phase.PULLED and not quotes(engine)
    pool.feed(engine, book_event(INTER, BIDS, ASKS), clock.now)
    await engine.step()
    assert not quotes(engine)


async def test_jump_pauses_then_resumes(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    clock.now += 1_000_000_000
    pool.feed(engine, book_event(INTER, [("0.55", "300")], [("0.57", "300")]), clock.now)
    await engine.step()
    assert engine.slots[INTER].phase is Phase.PAUSED and not quotes(engine)
    clock.now += 10 * MIN  # the EWMA caught up, the cooldown is over
    await engine.step()
    assert engine.slots[INTER].phase is Phase.QUOTING and quotes(engine)


async def test_dead_book_and_tick_change_take_quotes_off(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    pool.dead.add(INTER)
    clock.now += 1_000_000_000
    await engine.step()
    assert engine.slots[INTER].reason == "no_book" and not quotes(engine)
    pool.dead.clear()
    await engine.step()
    assert quotes(engine)
    pool.feed(engine, {"event_type": "tick_size_change", "asset_id": INTER}, clock.now)
    await engine.step()
    assert engine.slots[INTER].reason == "tick_change" and not quotes(engine)
    pool.feed(engine, book_event(INTER, BIDS, ASKS, tick="0.001"), clock.now)
    await engine.step()
    assert engine.slots[INTER].tick == D("0.001") and quotes(engine)


async def test_stale_metadata_pulls_quotes(tmp_path: Path) -> None:
    engine, _, clock = await quoting_engine(tmp_path)
    clock.now = engine.meta_fresh_until_ns + 1
    await engine.step()
    assert engine.slots[INTER].reason == "stale_metadata" and not quotes(engine)


async def test_daily_loss_limit_halts_until_the_next_day(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    notifier = engine.reporter.notifier
    # A bad inventory: 200 shares bought at 0.42, then the book drops to 0.30.
    holding = engine.venue.holding(INTER)
    holding.long, holding.cost = D(200), D(84)
    engine.venue.cash -= D(84)
    clock.now += 1_000_000_000
    pool.feed(engine, book_event(INTER, [("0.29", "300")], [("0.31", "300")]), clock.now)
    await engine.step()
    await engine.wait_reports()
    assert engine.day.halted and not quotes(engine)
    assert any("Дневной лимит" in text for text in notifier.texts())  # type: ignore[attr-defined]
    clock.now = T0 + 12 * H + MIN  # next UTC day: the limit resets
    await engine.step()
    assert not engine.day.halted


async def test_day_rollover_writes_the_report(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    clock.now += 200 * 1_000_000
    pool.feed(engine, book_event(INTER, BIDS, ASKS), clock.now)
    pool.feed(engine, trade_event(INTER, "0.38", "30"), clock.now)
    clock.now = T0 + 12 * H + MIN  # 2026-09-27 00:01 UTC
    await engine.step()
    await engine.wait_reports()
    report = (tmp_path / "reports" / "paper_2026-09-26.md").read_text(encoding="utf-8")
    assert "| Сделок | 1 |" in report and "Inter vs. Milan" in report
    daily = [(t, silent) for t, silent in engine.reporter.notifier.messages if "Итоги дня" in t]  # type: ignore[attr-defined]
    assert daily and daily[0][1] is True
    assert engine.day.day == "2026-09-27" and engine.day.fills == 0


async def test_settlement_pays_out_and_forgets_the_market(tmp_path: Path) -> None:
    clock = Clock()
    markets = {"10000": {"closed": False, "outcomePrices": '["0.6", "0.4"]'}}
    engine, pool, notifier, _ = make_engine(tmp_path, clock, markets=markets)
    chosen = select(pairs(raw_event("1000", START)), engine.cfg, clock.now, paper=True).chosen
    pool.set_assets(await engine.apply_selection(chosen))
    holding = engine.venue.holding(INTER)
    holding.long, holding.cost = D(20), D("7.80")
    engine.venue.cash -= D("7.80")
    clock.now = START + 3 * H
    await engine.settle_due()
    assert INTER in engine.venue.holdings  # not closed yet: wait
    markets["10000"] = {"closed": True, "outcomePrices": '["1", "0"]'}
    await engine.settle_due()
    await engine.wait_reports()
    assert INTER not in engine.venue.holdings and INTER not in engine.slots
    assert engine.venue.cash == D("212.20")
    assert engine.day.settlements[0].pnl == 12.2
    assert any("Рассчитан рынок" in text for text in notifier.texts())


def test_final_payouts_need_a_closed_market_and_a_full_payout() -> None:
    assert final_payouts({"closed": True, "outcomePrices": '["0", "1"]'}, 0) == (D(0), D(1))
    assert final_payouts({"closed": True, "outcomePrices": ["0.5", "0.5"]}, 1) == (
        D("0.5"),
        D("0.5"),
    )
    assert final_payouts({"closed": False, "outcomePrices": '["1", "0"]'}, 0) is None
    assert final_payouts({"closed": True, "outcomePrices": '["0.97", "0.02"]'}, 0) is None
    assert final_payouts({"closed": True, "outcomePrices": "oops"}, 0) is None


async def test_deselected_market_is_pulled_and_kept_while_exposed(tmp_path: Path) -> None:
    engine, _, _ = await quoting_engine(tmp_path)
    engine.venue.holding(INTER).long = D(20)
    kept = await engine.apply_selection([])
    assert kept == {INTER}  # position: keep the book for marks and settlement
    assert engine.slots[INTER].phase is Phase.PULLED and not quotes(engine)
    assert INTER in engine.active_tokens()  # held: first in line when selected again
    engine.venue.holding(INTER).long = D(0)
    engine.venue.drop_all()
    assert await engine.apply_selection([]) == set()


async def test_state_survives_a_restart(tmp_path: Path) -> None:
    engine, pool, clock = await quoting_engine(tmp_path)
    clock.now += 200 * 1_000_000
    pool.feed(engine, book_event(INTER, BIDS, ASKS), clock.now)
    pool.feed(engine, trade_event(INTER, "0.38", "30"), clock.now)
    engine.drop_all()
    engine.save_state()
    assert (tmp_path / "state" / STATE_FILE).exists()

    again, _, _, _ = make_engine(tmp_path, clock)
    assert again.load_state() is None  # same day: continue it
    assert again.venue.cash == engine.venue.cash
    assert again.venue.holdings[INTER].long == D(20)
    assert again.day.fills == 1 and again.known[INTER].title == "Inter vs. Milan"
    assert not again.venue.orders

    clock.now = T0 + 13 * H  # restarted on the next day: the saved day gets its report
    later, _, _, _ = make_engine(tmp_path, clock)
    closed = later.load_state()
    assert closed is not None and closed.stats.day == "2026-09-26" and closed.stats.fills == 1
    assert later.day.day == "2026-09-27"


async def test_paper_mode_only_quotes_unreviewed_rules_when_allowed(tmp_path: Path) -> None:
    cfg = mini_config(rules={"paper_quote_unreviewed": False})
    result = select(pairs(raw_event("1000", START)), cfg, T0, paper=True)
    assert not result.chosen and result.skipped["rules_not_reviewed"] == 3
    approved = mini_config(rules={"approved_templates": list(result.templates)})
    chosen = select(pairs(raw_event("1000", START)), approved, T0, paper=False).chosen
    assert len(chosen) == 3 and all(c.reviewed for c in chosen)

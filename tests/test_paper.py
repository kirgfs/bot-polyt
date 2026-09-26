"""Paper venue: post-only acceptance, latency, queue-position fills, cash and settlement."""

from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.execution.paper import PaperEvent, PaperFill, PaperRejected, PaperRules, PaperVenue
from polybot.venues.base import BookLevel, BookSnapshot, InstrumentRef, Side, TradePrint, VenueId

REF = InstrumentRef(VenueId.POLYMARKET, "0xc", "yes")
MS = 1_000_000
D = Decimal


class Clock:
    def __init__(self) -> None:
        self.now = 1_000 * MS

    def __call__(self) -> int:
        return self.now


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]], ts: int) -> BookSnapshot:
    return BookSnapshot(
        REF,
        [BookLevel(D(p), D(s)) for p, s in bids],
        [BookLevel(D(p), D(s)) for p, s in asks],
        None,
        ts,
    )


def trade(price: str, size: str, ts: int) -> TradePrint:
    return TradePrint(REF, D(price), D(size), None, None, ts)


def venue(
    cash: str = "100", **rules: object
) -> tuple[PaperVenue, Clock, list[PaperFill], list[PaperEvent]]:
    clock, fills, events = Clock(), [], []  # type: ignore[var-annotated]
    v = PaperVenue(
        cash=D(cash), latency_ns=100 * MS, clock=clock, on_fill=fills.append, on_event=events.append
    )
    v.rules[REF.instrument_id] = PaperRules(tick=D("0.01"), min_size=D(5), **rules)  # type: ignore[arg-type]
    return v, clock, fills, events


async def test_order_needs_latency_and_joins_the_back_of_the_queue() -> None:
    v, clock, fills, _ = venue()
    t0 = clock.now
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0))
    oid = await v.place_post_only(REF, Side.BUY, D("0.40"), D(20))
    v.on_trade(trade("0.40", "30", t0 + 50 * MS))  # before the order reached the book
    assert not fills and not v.orders[oid].active
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0 + 150 * MS))
    assert v.orders[oid].active and v.orders[oid].queue_ahead == D(100)
    v.on_trade(trade("0.40", "60", t0 + 200 * MS))  # queue ahead 100 → 40, no fill
    assert not fills and v.orders[oid].queue_ahead == D(40)
    v.on_book(book([("0.40", "25")], [("0.45", "50")], t0 + 250 * MS))  # cancels ahead of us
    assert v.orders[oid].queue_ahead == D(25)
    v.on_trade(trade("0.40", "30", t0 + 300 * MS))  # 25 ahead, then 5 to us
    assert [(f.size, f.price) for f in fills] == [(D(5), D("0.40"))]
    assert v.holding("yes").long == D(5) and v.cash == D(98)


async def test_trade_through_and_book_crossing_fill_at_our_price() -> None:
    v, clock, fills, _ = venue()
    t0 = clock.now
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0))
    await v.place_post_only(REF, Side.BUY, D("0.42"), D(20))
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0 + 150 * MS))
    v.on_trade(trade("0.39", "8", t0 + 160 * MS))  # sold below our bid: we were first
    assert fills[-1].size == D(8) and fills[-1].price == D("0.42")
    v.on_book(book([("0.40", "100")], [("0.42", "5"), ("0.45", "50")], t0 + 170 * MS))
    assert fills[-1].size == D(5)  # an ask at our price trades with us
    v.on_book(book([("0.40", "100")], [("0.42", "5"), ("0.45", "50")], t0 + 180 * MS))
    assert len(fills) == 2  # the same resting ask is not counted twice


async def test_post_only_reject_when_the_book_moved() -> None:
    v, clock, fills, events = venue()
    t0 = clock.now
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0))
    oid = await v.place_post_only(REF, Side.SELL, D("0.44"), D(10))
    v.on_book(book([("0.44", "10")], [("0.45", "50")], t0 + 150 * MS))  # bid rose to our ask
    assert oid not in v.orders and not fills
    assert events[-1].kind == "reject" and events[-1].reason == "INVALID_POST_ONLY_ORDER"
    assert v.locked_cash == 0


async def test_cancel_lands_after_latency() -> None:
    v, clock, fills, events = venue()
    t0 = clock.now
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0))
    oid = await v.place_post_only(REF, Side.BUY, D("0.41"), D(10))
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0 + 150 * MS))
    clock.now = t0 + 200 * MS
    await v.cancel([oid])
    v.on_trade(trade("0.39", "4", t0 + 250 * MS))  # still in the book: filled
    v.on_trade(trade("0.39", "4", t0 + 350 * MS))  # cancel landed at 300 ms
    assert [f.size for f in fills] == [D(4)]
    assert oid not in v.orders and events[-1].kind == "cancelled"
    assert v.locked_cash == 0


async def test_cash_limits_and_asks_without_inventory() -> None:
    v, clock, _, _ = venue(cash="10")
    v.on_book(book([("0.40", "100")], [("0.45", "50")], clock.now))
    with pytest.raises(PaperRejected, match="NOT_ENOUGH_BALANCE"):
        await v.place_post_only(REF, Side.BUY, D("0.40"), D(30))  # $12 > $10
    with pytest.raises(PaperRejected, match="MIN_TICK"):
        await v.place_post_only(REF, Side.BUY, D("0.405"), D(10))
    with pytest.raises(PaperRejected, match="MIN_SIZE"):
        await v.place_post_only(REF, Side.BUY, D("0.40"), D(4))
    await v.place_post_only(REF, Side.SELL, D("0.50"), D(10))  # like a NO bid at 0.50: $5
    assert v.locked_cash == D(5) and v.available_cash == D(5)


async def test_round_trip_merge_rebate_and_settlement() -> None:
    v, clock, fills, _ = venue(cash="100", fee_rate=0.05, fee_exponent=1.0, rebate_rate=0.15)
    t0 = clock.now
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0))
    await v.place_post_only(REF, Side.BUY, D("0.41"), D(10))
    await v.place_post_only(REF, Side.SELL, D("0.44"), D(10))
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0 + 150 * MS))
    v.on_trade(trade("0.40", "10", t0 + 200 * MS))  # our bid 0.41 filled (price through)
    v.on_trade(trade("0.45", "10", t0 + 210 * MS))  # our ask 0.44 filled
    # Bought 10 at 0.41; the ask was placed without YES, so it bought 10 NO at 0.56;
    # the pairs merged back into $10: net +$0.30 = 10 × (0.44 − 0.41).
    assert v.cash == D("100.30") and v.holding("yes").net == 0
    expected_rebate = 0.15 * 0.05 * (0.41 * 0.59 + 0.44 * 0.56) * 10
    assert abs(v.rebates - expected_rebate) < 1e-9
    await v.place_post_only(REF, Side.BUY, D("0.41"), D(10))
    v.on_book(book([("0.40", "100")], [("0.45", "50")], t0 + 400 * MS))
    v.on_trade(trade("0.40", "10", t0 + 450 * MS))
    assert abs(v.value({"yes": 0.425}) - (100.30 - 4.10 + 4.25)) < 1e-9
    pnl = v.settle("yes", long_payout=D(1), short_payout=D(0))  # our outcome won
    # P&L of the market over its life: 0.30 from the spread + 5.90 from the settlement.
    assert pnl == D("6.20") and v.cash == D("106.20") and "yes" not in v.holdings
    assert len(fills) == 3

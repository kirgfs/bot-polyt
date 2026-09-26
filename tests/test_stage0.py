"""Stage-0 quoter, reward scoring and risk limits: pure logic, no network."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from polybot.risk.limits import DailyLossGuard, cost_per_share, max_position_shares
from polybot.strategy import rewards
from polybot.strategy.stage0 import (
    FairValue,
    Quote,
    QuoteParams,
    RewardParams,
    compute_quotes,
    microprice,
    needs_requote,
    order_size,
)
from polybot.venues.base import BookLevel, BookSnapshot, InstrumentRef, Side, VenueId

REF = InstrumentRef(VenueId.POLYMARKET, "0xc", "yes-token")
TICK = Decimal("0.01")
PARAMS = QuoteParams(
    ewma_halflife_s=60,
    jump_ticks=3,
    jump_cooldown_s=60,
    max_book_spread=0.10,
    rewards_spread_fraction=0.6,
    default_half_spread_ticks=3,
    min_half_spread_ticks=1,
    requote_ticks=2,
    skew_ticks=2,
)


def book(bids: list[tuple[str, str]], asks: list[tuple[str, str]], ts: int = 0) -> BookSnapshot:
    return BookSnapshot(
        REF,
        [BookLevel(Decimal(p), Decimal(s)) for p, s in bids],
        [BookLevel(Decimal(p), Decimal(s)) for p, s in asks],
        None,
        ts,
    )


def quotes(b: BookSnapshot, fair: float, position: str = "0", **kw: object) -> dict[Side, Decimal]:
    args: dict[str, object] = {
        "book": b,
        "fair": fair,
        "tick": TICK,
        "rewards": None,
        "position": Decimal(position),
        "max_position": Decimal(100),
        "bid_size": Decimal(10),
        "ask_size": Decimal(10),
        "params": PARAMS,
    }
    args.update(kw)
    return {q.side: q.price for q in compute_quotes(**args)}  # type: ignore[arg-type]


def test_microprice_leans_to_the_thin_side() -> None:
    assert microprice(book([("0.40", "100")], [("0.44", "100")])) == 0.42
    # Big bid, small ask: the price is about to tick up.
    assert microprice(book([("0.40", "300")], [("0.44", "100")])) == 0.43
    assert microprice(book([("0.40", "1")], [])) is None


def test_symmetric_quotes_and_rewards_band() -> None:
    b = book([("0.40", "100")], [("0.50", "100")])
    assert quotes(b, 0.45) == {Side.BUY: Decimal("0.42"), Side.SELL: Decimal("0.48")}
    # Rewards v = 3¢: half-spread 0.6 × 3 = 1.8¢ → 0.432/0.468 → ticks 0.43/0.47.
    r = RewardParams(max_spread=0.03, min_size=50, daily_rate=10)
    assert quotes(b, 0.45, rewards=r) == {Side.BUY: Decimal("0.43"), Side.SELL: Decimal("0.47")}


def test_post_only_never_touches_the_other_side() -> None:
    b = book([("0.44", "100")], [("0.45", "100")])
    q = quotes(b, 0.46)  # fair above the book: bid would cross without the clamp
    assert q[Side.BUY] <= Decimal("0.44") and q[Side.SELL] >= Decimal("0.45")


def test_inventory_skew_and_limits() -> None:
    b = book([("0.40", "100")], [("0.50", "100")])
    flat, long_half = quotes(b, 0.45), quotes(b, 0.45, position="50")
    assert long_half[Side.BUY] < flat[Side.BUY] and long_half[Side.SELL] < flat[Side.SELL]
    assert Side.BUY not in quotes(b, 0.45, position="100")  # at the cap: only reduce
    assert Side.SELL not in quotes(b, 0.45, position="-100")


def test_no_quotes_on_bad_books() -> None:
    assert quotes(book([("0.40", "1")], []), 0.4) == {}
    assert quotes(book([("0.30", "1")], [("0.50", "1")]), 0.4) == {}  # 20¢ > max spread
    assert quotes(book([("0.40", "1")], [("0.50", "1")]), None) == {}  # type: ignore[arg-type]


@settings(max_examples=300, deadline=None)
@given(
    bid_ticks=st.integers(min_value=1, max_value=97),
    width=st.integers(min_value=1, max_value=10),
    fair_offset=st.floats(min_value=-0.05, max_value=0.05),
    position=st.integers(min_value=-150, max_value=150),
    v=st.one_of(st.none(), st.floats(min_value=0.005, max_value=0.08)),
)
def test_quote_invariants(
    bid_ticks: int, width: int, fair_offset: float, position: int, v: float | None
) -> None:
    best_bid = Decimal(bid_ticks) * TICK
    best_ask = min(best_bid + width * TICK, Decimal("0.99"))
    if best_ask <= best_bid:
        return
    b = book([(str(best_bid), "10")], [(str(best_ask), "10")])
    fair = float(best_bid + best_ask) / 2 + fair_offset
    r = RewardParams(v, 10, 1) if v is not None else None
    result = compute_quotes(
        book=b,
        fair=fair,
        tick=TICK,
        rewards=r,
        position=Decimal(position),
        max_position=Decimal(100),
        bid_size=Decimal(10),
        ask_size=Decimal(10),
        params=PARAMS,
    )
    sides = [q.side for q in result]
    assert len(sides) == len(set(sides))
    for q in result:
        assert q.price % TICK == 0 and TICK <= q.price <= 1 - TICK
        if q.side is Side.BUY:
            assert q.price < best_ask and position < 100
        else:
            assert q.price > best_bid and position > -100
    prices = {q.side: q.price for q in result}
    if len(prices) == 2:
        assert prices[Side.BUY] < prices[Side.SELL]


def test_fair_value_ewma_and_jump() -> None:
    fv = FairValue(halflife_s=10)
    fv.update(book([("0.40", "1")], [("0.42", "1")], ts=0))
    assert fv.value is not None and abs(fv.value - 0.41) < 1e-9
    fv.update(book([("0.50", "1")], [("0.52", "1")], ts=10 * 10**9))  # one half-life later
    assert fv.value is not None and abs(fv.value - 0.46) < 1e-9
    assert fv.jumped(0.01, 3)  # micro 0.51 vs fair 0.46: 5 ticks
    fv.update(book([("0.50", "1")], [("0.52", "1")], ts=60 * 10**9))
    assert not fv.jumped(0.01, 3)


def test_order_size_prefers_rewards_minimum() -> None:
    size = order_size(
        cost_per_share=Decimal("0.4"),
        min_order_size=Decimal(5),
        rewards_min_size=50,
        max_order_usd=25,
    )
    assert size == Decimal(50)  # 50 × 0.4 = $20 ≤ $25
    size = order_size(
        cost_per_share=Decimal("0.6"),
        min_order_size=Decimal(5),
        rewards_min_size=50,
        max_order_usd=25,
    )
    assert size == Decimal(5)  # rewards size unaffordable: exchange minimum
    assert (
        order_size(
            cost_per_share=Decimal("0.6"),
            min_order_size=Decimal(50),
            rewards_min_size=None,
            max_order_usd=25,
        )
        == 0
    )


def test_requote_threshold_keeps_queue_position() -> None:
    a = Quote(Side.BUY, Decimal("0.40"), Decimal(10))
    assert not needs_requote(a, Quote(Side.BUY, Decimal("0.41"), Decimal(10)), TICK, 2)
    assert needs_requote(a, Quote(Side.BUY, Decimal("0.42"), Decimal(10)), TICK, 2)
    assert needs_requote(a, None, TICK, 2) and not needs_requote(None, None, TICK, 2)


def test_reward_scoring() -> None:
    assert rewards.order_score(0.03, 0.0, 100) == 100
    assert rewards.order_score(0.03, 0.03, 100) == 0
    assert abs(rewards.order_score(0.03, 0.015, 100) - 25) < 1e-9
    # Inside [0.10, 0.90] one side still earns a third; outside both sides are needed.
    assert rewards.q_min(90, 0, 0.5) == 30 and rewards.q_min(90, 0, 0.95) == 0
    b = book([("0.49", "100")], [("0.51", "100")])
    s = rewards.sample(b, [(0.49, 100.0)], [(0.51, 100.0)], max_spread=0.03, min_size=50)
    assert s is not None and abs(s.share - 0.5) < 1e-9  # same quotes as the whole book
    small = rewards.sample(b, [(0.49, 10.0)], [(0.51, 10.0)], max_spread=0.03, min_size=50)
    assert small is not None and small.share == 0  # below rewardsMinSize: no score


def test_risk_limits() -> None:
    guard = DailyLossGuard(limit_usd=20)
    assert not guard.update(date(2026, 9, 26), 200)
    assert guard.update(date(2026, 9, 26), 179)
    assert not guard.update(date(2026, 9, 27), 179)  # new day, new baseline
    assert max_position_shares(40, 0.4) == Decimal(100)
    assert cost_per_share(Side.BUY, Decimal("0.4"), Decimal(0), Decimal(10)) == Decimal("0.4")
    # Ask for 10 with 4 YES in hand: 6 shares are covered like a NO bid at 0.6.
    assert cost_per_share(Side.SELL, Decimal("0.4"), Decimal(4), Decimal(10)) == Decimal("0.36")

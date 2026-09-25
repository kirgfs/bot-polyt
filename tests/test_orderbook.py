from __future__ import annotations

from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from polybot.venues.polymarket.orderbook import (
    BookTracker,
    DesyncReason,
    compare_with_rest,
    iter_events,
)

A = "1111111111111111111111111111111111111111111111111111111111111111111111111101"
CID = "0x" + "01" * 32


def book_event(
    bids: list[tuple[str, str]], asks: list[tuple[str, str]], h: str = "h0"
) -> dict[str, Any]:
    return {
        "event_type": "book",
        "market": CID,
        "asset_id": A,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "hash": h,
        "timestamp": "1787015700000",
    }


def change(price: str, size: str, side: str, bb: str, ba: str, h: str = "h1") -> dict[str, Any]:
    return {
        "asset_id": A,
        "price": price,
        "size": size,
        "side": side,
        "hash": h,
        "best_bid": bb,
        "best_ask": ba,
    }


def price_change(*changes: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": "price_change",
        "market": CID,
        "price_changes": list(changes),
        "timestamp": "1787015701000",
    }


def tracker_with_book() -> BookTracker:
    tracker = BookTracker()
    tracker.expect_snapshot(A)
    assert tracker.on_event(book_event([(".48", "30"), ("0.47", "10")], [("0.52", "25")]), 1) == []
    return tracker


def test_snapshot_and_levels() -> None:
    tracker = tracker_with_book()
    book = tracker.books[A]
    assert book.ready and book.best_bid() == Decimal("0.48") and book.best_ask() == Decimal("0.52")
    assert book.last_hash == "h0"


def test_price_change_consistent() -> None:
    tracker = tracker_with_book()
    desyncs = tracker.on_event(price_change(change("0.49", "5", "BUY", "0.49", "0.52")), 2)
    assert desyncs == []
    assert tracker.books[A].best_bid() == Decimal("0.49")
    assert tracker.books[A].last_hash == "h1"


def test_size_zero_removes_level() -> None:
    tracker = tracker_with_book()
    assert tracker.on_event(price_change(change("0.48", "0", "BUY", "0.47", "0.52")), 2) == []
    assert Decimal("0.48") not in tracker.books[A].bids


def test_top_mismatch_triggers_resync_and_reset() -> None:
    tracker = tracker_with_book()
    desyncs = tracker.on_event(price_change(change("0.49", "5", "BUY", "0.50", "0.52")), 2)
    assert [d.reason for d in desyncs] == [DesyncReason.TOP_MISMATCH]
    assert not tracker.books[A].ready
    # Deltas are ignored until the next snapshot.
    tracker.on_event(price_change(change("0.45", "5", "BUY", "0.45", "0.52")), 3)
    assert tracker.stats.deltas_before_snapshot == 1


def test_empty_side_sentinels() -> None:
    tracker = BookTracker()
    tracker.expect_snapshot(A)
    tracker.on_event(book_event([("0.40", "1")], []), 1)
    # Removing the only bid: server reports "0" for an empty bid side and "1" for no asks.
    assert tracker.on_event(price_change(change("0.40", "0", "BUY", "0", "1")), 2) == []
    assert tracker.on_event(price_change(change("0.60", "3", "SELL", "", "0.6")), 3) == []


def test_crossed_book_is_desync() -> None:
    tracker = tracker_with_book()
    desyncs = tracker.on_event(price_change(change("0.55", "1", "BUY", "0.55", "0.52")), 2)
    assert [d.reason for d in desyncs] == [DesyncReason.CROSSED]


def test_tick_size_change_starts_new_epoch() -> None:
    tracker = tracker_with_book()
    event = {
        "event_type": "tick_size_change",
        "asset_id": A,
        "old_tick_size": "0.01",
        "new_tick_size": "0.001",
    }
    assert [d.reason for d in tracker.on_event(event, 2)] == [DesyncReason.TICK_SIZE_CHANGE]
    assert not tracker.books[A].ready


def test_unknown_asset_is_ignored() -> None:
    tracker = BookTracker()
    assert tracker.on_event(book_event([("0.4", "1")], []), 1) == []
    assert A not in tracker.books


def test_frame_can_be_array() -> None:
    frame = '[{"event_type": "book", "asset_id": "1"}, {"event_type": "book", "asset_id": "2"}]'
    assert [e["asset_id"] for e in iter_events(frame)] == ["1", "2"]


def test_compare_with_rest() -> None:
    tracker = tracker_with_book()
    book = tracker.books[A]
    same = {
        "asset_id": A,
        "hash": "h0",
        "bids": [{"price": "0.47", "size": "10"}, {"price": "0.48", "size": "30"}],
        "asks": [{"price": "0.52", "size": "25"}],
    }
    result = compare_with_rest(book, same)
    assert result is not None and result.hash_equal and result.levels_equal
    wrong = dict(same, asks=[{"price": "0.53", "size": "25"}])
    result = compare_with_rest(book, wrong)
    assert result is not None and result.levels_equal is False
    moved = dict(same, hash="other")
    result = compare_with_rest(book, moved)
    assert result is not None and not result.hash_equal and result.levels_equal is None


prices = st.integers(min_value=1, max_value=99)
sizes = st.integers(min_value=0, max_value=50)
ops = st.lists(st.tuples(st.sampled_from(["BUY", "SELL"]), prices, sizes), min_size=1, max_size=60)


@settings(max_examples=200)
@given(ops)
def test_matches_reference_model_when_server_tops_are_right(
    sequence: list[tuple[str, int, int]],
) -> None:
    """Replaying any level updates with the true best bid/ask never raises a false desync."""
    tracker = BookTracker()
    tracker.expect_snapshot(A)
    tracker.on_event(book_event([], []), 0)
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    for step, (side, cents, size) in enumerate(sequence, start=1):
        levels = bids if side == "BUY" else asks
        crosses = (side == "BUY" and asks and cents >= min(asks)) or (
            side == "SELL" and bids and cents <= max(bids)
        )
        if size and crosses:
            continue  # the exchange never produces a crossed book
        if size:
            levels[cents] = size
        else:
            levels.pop(cents, None)
        best_bid = f"0.{max(bids):02d}" if bids else "0"
        best_ask = f"0.{min(asks):02d}" if asks else "1"
        msg = price_change(change(f"0.{cents:02d}", str(size), side, best_bid, best_ask))
        assert tracker.on_event(msg, step) == []
    book = tracker.books[A]
    assert {int(p * 100): int(s) for p, s in book.bids.items()} == bids
    assert {int(p * 100): int(s) for p, s in book.asks.items()} == asks

"""L2 order book per token, rebuilt from market WS events, with integrity checks.

Semantics (docs/api_notes.md §10): `book` replaces the book; `price_change` sets the
full size of a level (`"0"` deletes it) and carries the server's best bid/ask after the
change; `tick_size_change` starts a new book epoch. There are no sequence numbers, so
integrity is checked against `best_bid`/`best_ask` in every change and against REST
`/book` when the hashes are equal.

Prices and sizes stay Decimal (exchange boundary, CLAUDE.md). Only the current state of
each book is kept, never its history. Price objects are interned: prices lie on a small
tick grid, so thousands of books share a few hundred Decimal keys instead of holding
one object per level (a Decimal is ~100 bytes).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

_ZERO = Decimal(0)
_ONE = Decimal(1)


class DesyncReason(StrEnum):
    TOP_MISMATCH = "top_mismatch"  # our best bid/ask != best_bid/best_ask in the message
    CROSSED = "crossed"  # best bid >= best ask
    TICK_SIZE_CHANGE = "tick_size_change"  # new epoch: old levels may be off-grid
    REST_MISMATCH = "rest_mismatch"  # same hash as REST, different levels
    BAD_MESSAGE = "bad_message"  # unparseable numbers in a message for this asset


def to_decimal(value: object) -> Decimal | None:
    """Wire numbers are strings like "0.48" or ".48"; "" means absent."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


# Distinct wire spellings of prices seen so far ("0.48", ".48", "0.480"...). Bounded: past
# the cap, new spellings are parsed but not cached, so a hostile feed cannot grow it.
_PRICE_CACHE: dict[str, Decimal] = {}
_PRICE_CACHE_MAX = 20_000


def to_price(value: object) -> Decimal | None:
    """`to_decimal` for prices, returning one shared object per spelling."""
    if not isinstance(value, str):
        return to_decimal(value)
    cached = _PRICE_CACHE.get(value)
    if cached is not None:
        return cached
    result = to_decimal(value)
    if result is not None and len(_PRICE_CACHE) < _PRICE_CACHE_MAX:
        _PRICE_CACHE[value] = result
    return result


@dataclass(slots=True)
class L2Book:
    asset_id: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    ready: bool = False  # False until a snapshot arrives (and after an epoch reset)
    last_hash: str | None = None
    last_server_ts_ms: int | None = None
    last_update_ns: int = 0

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False
        self.last_hash = None

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def is_crossed(self) -> bool:
        bid, ask = self.best_bid(), self.best_ask()
        return bid is not None and ask is not None and bid >= ask

    def set_level(self, side: str, price: Decimal, size: Decimal) -> None:
        levels = self.bids if side == "BUY" else self.asks
        if size == _ZERO:
            levels.pop(price, None)
        else:
            levels[price] = size

    def snapshot(
        self, bids: Iterable[tuple[Decimal, Decimal]], asks: Iterable[tuple[Decimal, Decimal]]
    ) -> None:
        self.bids = {p: s for p, s in bids if s != _ZERO}
        self.asks = {p: s for p, s in asks if s != _ZERO}
        self.ready = True

    def levels(self) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        """(bids best-first, asks best-first)."""
        return (
            sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True),
            sorted(self.asks.items(), key=lambda kv: kv[0]),
        )


def _normalize_top(value: Decimal | None, empty_sentinel: Decimal) -> Decimal | None:
    # An empty side is reported as absent, "0" (bid) or "1" (ask): neither is a valid
    # price because prices lie in [tick, 1 - tick] (docs/api_notes.md §4).
    if value is None or value == empty_sentinel:
        return None
    return value


def parse_levels(raw: object) -> list[tuple[Decimal, Decimal]] | None:
    if not isinstance(raw, list):
        return None
    out: list[tuple[Decimal, Decimal]] = []
    for level in raw:
        if not isinstance(level, dict):
            return None
        price, size = to_price(level.get("price")), to_decimal(level.get("size"))
        if price is None or size is None:
            return None
        out.append((price, size))
    return out


@dataclass(frozen=True, slots=True)
class Desync:
    asset_id: str
    reason: DesyncReason
    detail: str = ""


@dataclass
class TrackerStats:
    snapshots: int = 0
    changes: int = 0
    deltas_before_snapshot: int = 0
    top_checks: int = 0
    desyncs: dict[str, int] = field(default_factory=dict)
    trades: int = 0
    unknown_events: int = 0

    def count_desync(self, reason: DesyncReason) -> None:
        self.desyncs[reason.value] = self.desyncs.get(reason.value, 0) + 1


def iter_events(frame: str) -> Iterator[dict[str, Any]]:
    """A market WS frame is one event object or an array of them."""
    data = json.loads(frame)
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict):
            yield item


class BookTracker:
    """Maintains books for subscribed tokens and reports integrity violations."""

    def __init__(self) -> None:
        self.books: dict[str, L2Book] = {}
        self.stats = TrackerStats()

    def book(self, asset_id: str) -> L2Book:
        book = self.books.get(asset_id)
        if book is None:
            book = self.books[asset_id] = L2Book(asset_id)
        return book

    def forget(self, asset_id: str) -> None:
        self.books.pop(asset_id, None)

    def expect_snapshot(self, asset_id: str) -> None:
        """Called on (re)subscribe: deltas are ignored until the next `book` event."""
        self.book(asset_id).reset()

    def on_event(self, event: dict[str, Any], ts_recv_ns: int) -> list[Desync]:
        event_type = event.get("event_type") or event.get("type")
        if event_type == "book":
            return self._on_book(event, ts_recv_ns)
        if event_type == "price_change":
            return self._on_price_change(event, ts_recv_ns)
        if event_type == "tick_size_change":
            asset_id = str(event.get("asset_id") or "")
            if asset_id in self.books:
                self.books[asset_id].reset()
                self.stats.count_desync(DesyncReason.TICK_SIZE_CHANGE)
                return [Desync(asset_id, DesyncReason.TICK_SIZE_CHANGE)]
            return []
        if event_type == "last_trade_price":
            self.stats.trades += 1
            return []
        if event_type in ("best_bid_ask", "new_market", "market_resolved"):
            return []
        self.stats.unknown_events += 1
        return []

    def _on_book(self, event: dict[str, Any], ts_recv_ns: int) -> list[Desync]:
        asset_id = str(event.get("asset_id") or "")
        if asset_id not in self.books:
            return []  # not ours (or already unsubscribed)
        bids, asks = parse_levels(event.get("bids")), parse_levels(event.get("asks"))
        book = self.books[asset_id]
        if bids is None or asks is None:
            book.reset()
            self.stats.count_desync(DesyncReason.BAD_MESSAGE)
            return [Desync(asset_id, DesyncReason.BAD_MESSAGE, "book levels")]
        book.snapshot(bids, asks)
        book.last_hash = _opt_str(event.get("hash"))
        book.last_server_ts_ms = _opt_int(event.get("timestamp"))
        book.last_update_ns = ts_recv_ns
        self.stats.snapshots += 1
        if book.is_crossed():
            book.reset()
            self.stats.count_desync(DesyncReason.CROSSED)
            return [Desync(asset_id, DesyncReason.CROSSED, "snapshot")]
        return []

    def _on_price_change(self, event: dict[str, Any], ts_recv_ns: int) -> list[Desync]:
        changes = event.get("price_changes")
        if not isinstance(changes, list):
            return []
        # Group per asset, keep message order; check the top after the whole batch.
        by_asset: dict[str, list[dict[str, Any]]] = {}
        for change in changes:
            if isinstance(change, dict):
                by_asset.setdefault(str(change.get("asset_id") or ""), []).append(change)
        desyncs: list[Desync] = []
        server_ts = _opt_int(event.get("timestamp"))
        for asset_id, asset_changes in by_asset.items():
            book = self.books.get(asset_id)
            if book is None:
                continue
            if not book.ready:
                self.stats.deltas_before_snapshot += len(asset_changes)
                continue
            desync = self._apply_changes(book, asset_changes)
            book.last_server_ts_ms = server_ts
            book.last_update_ns = ts_recv_ns
            if desync is not None:
                book.reset()
                self.stats.count_desync(desync.reason)
                desyncs.append(desync)
        return desyncs

    def _apply_changes(self, book: L2Book, changes: list[dict[str, Any]]) -> Desync | None:
        for change in changes:
            price, size = to_price(change.get("price")), to_decimal(change.get("size"))
            side = str(change.get("side") or "").upper()
            if price is None or size is None or side not in ("BUY", "SELL"):
                return Desync(book.asset_id, DesyncReason.BAD_MESSAGE, "price_change fields")
            book.set_level(side, price, size)
            self.stats.changes += 1
            if change.get("hash"):
                book.last_hash = str(change["hash"])
        last = changes[-1]
        if "best_bid" in last or "best_ask" in last:
            self.stats.top_checks += 1
            expected_bid = _normalize_top(to_price(last.get("best_bid")), _ZERO)
            expected_ask = _normalize_top(to_price(last.get("best_ask")), _ONE)
            ours = (book.best_bid(), book.best_ask())
            if ours != (expected_bid, expected_ask):
                return Desync(
                    book.asset_id,
                    DesyncReason.TOP_MISMATCH,
                    f"ours={ours[0]}/{ours[1]} server={expected_bid}/{expected_ask}",
                )
        if book.is_crossed():
            return Desync(book.asset_id, DesyncReason.CROSSED, "after price_change")
        return None


@dataclass(frozen=True, slots=True)
class RestComparison:
    asset_id: str
    hash_equal: bool
    levels_equal: bool | None  # None when hashes differ (the book moved in between)


def compare_with_rest(book: L2Book, rest: dict[str, Any]) -> RestComparison | None:
    """Compare our book with a REST `/book` object. None if the REST object is unusable."""
    rest_hash = _opt_str(rest.get("hash"))
    bids, asks = parse_levels(rest.get("bids")), parse_levels(rest.get("asks"))
    if bids is None or asks is None or not book.ready:
        return None
    if rest_hash is None or rest_hash != book.last_hash:
        return RestComparison(book.asset_id, hash_equal=False, levels_equal=None)
    rest_bids = {p: s for p, s in bids if s != _ZERO}
    rest_asks = {p: s for p, s in asks if s != _ZERO}
    return RestComparison(
        book.asset_id,
        hash_equal=True,
        levels_equal=rest_bids == book.bids and rest_asks == book.asks,
    )


def _opt_str(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _opt_int(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None

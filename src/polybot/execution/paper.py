"""Paper execution venue: post-only orders against live L2 books, fills from real trades.

Nothing leaves the process: this is the only execution venue in the code base, and it
talks to no exchange (CLAUDE.md, rule 1). The model (docs/architecture.md §13):

- an order reaches the book `latency` after it is placed; if it would cross the book at
  that moment, it is rejected, as a post-only order would be (docs/api_notes.md §4);
- queue position is estimated from L2 only (docs/api_notes.md §10: no L3): the order joins
  the back of its price level; the level shrinking moves it up; trades at its price
  consume the queue ahead first;
- a trade strictly through our price, or the opposite best price reaching it, fills us at
  our price: our order would have been first in line;
- a cancel lands `latency` after it is sent; fills in between still count;
- cash: a bid locks price × size; an ask sells YES we hold, the rest is covered like a NO
  bid at (1 − price); YES + NO pairs merge back into cash, $1 per pair;
- the maker pays no fee; the rebate estimate is rebate_rate × fee_rate × (p(1 − p))^e per
  share (docs/api_notes.md §8), zero when the market does not publish the parameters.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from polybot.venues.base import BookLevel, BookSnapshot, InstrumentRef, Side, TradePrint, VenueId

ZERO = Decimal(0)
ONE = Decimal(1)
SIZE_STEP = Decimal("0.01")


class PaperRejected(Exception):
    """Order refused, with the exchange's error name where one exists (docs/api_notes.md §4)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PaperRules:
    tick: Decimal
    min_size: Decimal
    fee_rate: float | None = None
    fee_exponent: float | None = None
    rebate_rate: float | None = None


@dataclass
class PaperOrder:
    order_id: str
    ref: InstrumentRef
    side: Side
    price: Decimal
    size: Decimal
    placed_ns: int
    active_from_ns: int
    expires_ns: int | None = None
    filled: Decimal = ZERO
    active: bool = False
    queue_ahead: Decimal = ZERO
    crossing_seen: Decimal = ZERO
    cancel_at_ns: int | None = None
    locked_cash: Decimal = ZERO
    locked_long: Decimal = ZERO

    @property
    def remaining(self) -> Decimal:
        return self.size - self.filled


@dataclass(frozen=True, slots=True)
class PaperFill:
    order_id: str
    ref: InstrumentRef
    side: Side
    price: Decimal
    size: Decimal
    ts_ns: int
    rebate: float


@dataclass
class Holding:
    """Shares of the quoted outcome (long) and of its complement (short side, like NO)."""

    long: Decimal = ZERO
    short: Decimal = ZERO
    cost: Decimal = ZERO  # net cash paid into this instrument

    @property
    def net(self) -> Decimal:
        return self.long - self.short


@dataclass
class PaperEvent:
    kind: str  # "reject" | "cancelled" | "expired"
    order: PaperOrder
    ts_ns: int
    reason: str = ""


FillHandler = Callable[[PaperFill], None]
EventHandler = Callable[[PaperEvent], None]


@dataclass
class PaperVenue:
    """ExecutionVenue (polybot.venues.base) that simulates fills instead of sending orders."""

    cash: Decimal
    latency_ns: int
    clock: Callable[[], int]
    on_fill: FillHandler | None = None
    on_event: EventHandler | None = None
    venue: VenueId = VenueId.POLYMARKET
    books: dict[str, BookSnapshot] = field(default_factory=dict)
    rules: dict[str, PaperRules] = field(default_factory=dict)
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    holdings: dict[str, Holding] = field(default_factory=dict)
    locked_cash: Decimal = ZERO
    rebates: float = 0.0
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    # ------------------------------------------------------------------ order entry

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.locked_cash

    def holding(self, instrument_id: str) -> Holding:
        return self.holdings.setdefault(instrument_id, Holding())

    def free_long(self, instrument_id: str) -> Decimal:
        locked = sum(
            (o.locked_long for o in self.orders.values() if o.ref.instrument_id == instrument_id),
            ZERO,
        )
        return self.holding(instrument_id).long - locked

    async def place_post_only(
        self,
        ref: InstrumentRef,
        side: Side,
        price: Decimal,
        size: Decimal,
        expires_ns: int | None = None,
    ) -> str:
        rules = self.rules.get(ref.instrument_id)
        if rules is None:
            raise PaperRejected("MARKET_NOT_READY", "no rules for the instrument")
        if price % rules.tick != 0 or not rules.tick <= price <= ONE - rules.tick:
            raise PaperRejected("INVALID_ORDER_MIN_TICK_SIZE", f"price {price}")
        size = size.quantize(SIZE_STEP, rounding="ROUND_DOWN")
        if size < rules.min_size or size <= 0:
            raise PaperRejected("INVALID_ORDER_MIN_SIZE", f"size {size}")
        locked_long = ZERO
        if side is Side.BUY:
            need = price * size
        else:
            locked_long = min(max(self.free_long(ref.instrument_id), ZERO), size)
            need = (ONE - price) * (size - locked_long)
        if need > self.available_cash:
            raise PaperRejected("INVALID_ORDER_NOT_ENOUGH_BALANCE", f"need {need}")
        now = self.clock()
        order = PaperOrder(
            order_id=f"paper-{next(self._ids)}",
            ref=ref,
            side=side,
            price=price,
            size=size,
            placed_ns=now,
            active_from_ns=now + self.latency_ns,
            expires_ns=expires_ns,
            locked_cash=need,
            locked_long=locked_long,
        )
        self.locked_cash += need
        self.orders[order.order_id] = order
        return order.order_id

    async def cancel(self, order_ids: Sequence[str]) -> None:
        cancel_at = self.clock() + self.latency_ns
        for order_id in order_ids:
            order = self.orders.get(order_id)
            if order is not None and order.cancel_at_ns is None:
                order.cancel_at_ns = cancel_at

    async def cancel_market(self, market_id: str) -> None:
        await self.cancel(
            [o.order_id for o in self.orders.values() if o.ref.market_id == market_id]
        )

    async def cancel_all(self) -> None:
        await self.cancel(list(self.orders))

    def drop_all(self) -> None:
        """Immediate removal (process start/stop): open paper orders do not outlive the run."""
        for order in list(self.orders.values()):
            self._remove(order, "cancelled", self.clock())

    def open_orders(self, instrument_id: str) -> list[PaperOrder]:
        return [
            o
            for o in self.orders.values()
            if o.ref.instrument_id == instrument_id and o.cancel_at_ns is None
        ]

    # ------------------------------------------------------------------ market data

    def on_book(self, book: BookSnapshot) -> list[PaperFill]:
        instrument = book.ref.instrument_id
        self.books[instrument] = book
        ts = book.ts_recv_ns
        self._activate(instrument, ts)
        fills: list[PaperFill] = []
        for order in self._live(instrument, ts):
            same = book.bids if order.side is Side.BUY else book.asks
            order.queue_ahead = min(order.queue_ahead, _level_size(same, order.price))
            crossing = _crossing_size(book, order)
            fresh = crossing - order.crossing_seen
            order.crossing_seen = crossing
            if fresh > 0:
                fills.append(self._fill(order, min(order.remaining, fresh), ts))
        self._expire(instrument, ts)
        return fills

    def on_trade(self, trade: TradePrint) -> list[PaperFill]:
        instrument = trade.ref.instrument_id
        ts = trade.ts_recv_ns
        self._activate(instrument, ts)
        left = trade.size
        fills: list[PaperFill] = []
        live = self._live(instrument, ts)
        # Best-priced orders first: a taker sweeps them in price order.
        live.sort(key=lambda o: -o.price if o.side is Side.BUY else o.price)
        for order in live:
            if left <= 0:
                break
            through = (
                trade.price < order.price if order.side is Side.BUY else trade.price > order.price
            )
            if through:
                take = min(order.remaining, left)
            elif trade.price == order.price:
                take = min(order.remaining, max(ZERO, left - order.queue_ahead))
                order.queue_ahead = max(ZERO, order.queue_ahead - left)
            else:
                continue
            if take > 0:
                fills.append(self._fill(order, take, ts))
                left -= take
        self._expire(instrument, ts)
        return fills

    def advance(self, now_ns: int) -> None:
        """Activate, cancel and expire orders by time alone (quiet markets)."""
        for instrument in {o.ref.instrument_id for o in self.orders.values()}:
            self._activate(instrument, now_ns)
            self._expire(instrument, now_ns)

    # ------------------------------------------------------------------ portfolio

    def settle(self, instrument_id: str, long_payout: Decimal, short_payout: Decimal) -> Decimal:
        """Pay out a resolved market; returns its P&L over the run (payout − net cash paid in)."""
        for order in [o for o in self.orders.values() if o.ref.instrument_id == instrument_id]:
            self._remove(order, "cancelled", self.clock())
        holding = self.holdings.pop(instrument_id, None)
        if holding is None:
            return ZERO
        payout = holding.long * long_payout + holding.short * short_payout
        self.cash += payout
        return payout - holding.cost

    def value(self, marks: dict[str, float]) -> float:
        """Cash plus positions at `marks` (price of the long outcome per instrument)."""
        total = float(self.cash)
        for instrument, holding in self.holdings.items():
            mark = marks.get(instrument)
            if mark is None:
                mark = (
                    float(holding.cost / (holding.long + holding.short))
                    if holding.long + holding.short
                    else 0.0
                )
                total += float(holding.long + holding.short) * mark
                continue
            total += float(holding.long) * mark + float(holding.short) * (1.0 - mark)
        return total

    # ------------------------------------------------------------------ internals

    def _live(self, instrument: str, ts: int) -> list[PaperOrder]:
        return [
            o
            for o in self.orders.values()
            if o.ref.instrument_id == instrument
            and o.active
            and (o.cancel_at_ns is None or ts < o.cancel_at_ns)
            and (o.expires_ns is None or ts < o.expires_ns)
        ]

    def _activate(self, instrument: str, ts: int) -> None:
        book = self.books.get(instrument)
        for order in [o for o in self.orders.values() if o.ref.instrument_id == instrument]:
            if order.active or order.active_from_ns > ts:
                continue
            if order.cancel_at_ns is not None and order.cancel_at_ns <= order.active_from_ns:
                continue  # cancelled before it reached the book
            if book is None:
                continue
            opposite = book.asks if order.side is Side.BUY else book.bids
            if opposite and (
                order.price >= opposite[0].price
                if order.side is Side.BUY
                else order.price <= opposite[0].price
            ):
                self._remove(order, "reject", ts, "INVALID_POST_ONLY_ORDER")
                continue
            order.active = True
            same = book.bids if order.side is Side.BUY else book.asks
            order.queue_ahead = _level_size(same, order.price)

    def _expire(self, instrument: str, ts: int) -> None:
        for order in [o for o in self.orders.values() if o.ref.instrument_id == instrument]:
            if order.cancel_at_ns is not None and ts >= order.cancel_at_ns:
                self._remove(order, "cancelled", ts)
            elif order.expires_ns is not None and ts >= order.expires_ns:
                self._remove(order, "expired", ts)

    def _remove(self, order: PaperOrder, kind: str, ts: int, reason: str = "") -> None:
        if self.orders.pop(order.order_id, None) is None:
            return
        self.locked_cash -= order.locked_cash
        order.locked_cash = ZERO
        order.locked_long = ZERO
        if self.on_event is not None:
            self.on_event(PaperEvent(kind, order, ts, reason))

    def _fill(self, order: PaperOrder, qty: Decimal, ts: int) -> PaperFill:
        holding = self.holding(order.ref.instrument_id)
        order.filled += qty
        if order.side is Side.BUY:
            cost = order.price * qty
            order.locked_cash -= cost
            self.locked_cash -= cost
            self.cash -= cost
            holding.long += qty
            holding.cost += cost
        else:
            from_long = min(qty, order.locked_long)
            proceeds = order.price * from_long
            order.locked_long -= from_long
            holding.long -= from_long
            self.cash += proceeds
            holding.cost -= proceeds
            rest = qty - from_long
            cost = (ONE - order.price) * rest
            order.locked_cash -= cost
            self.locked_cash -= cost
            self.cash -= cost
            holding.short += rest
            holding.cost += cost
        pairs = min(holding.long, holding.short)
        if pairs > 0:  # merge YES + NO back into collateral
            holding.long -= pairs
            holding.short -= pairs
            self.cash += pairs
            holding.cost -= pairs
        fill = PaperFill(
            order.order_id, order.ref, order.side, order.price, qty, ts, self._rebate(order, qty)
        )
        self.rebates += fill.rebate
        if order.remaining <= 0:
            self._remove(order, "filled", ts)
        if self.on_fill is not None:
            self.on_fill(fill)
        return fill

    def _rebate(self, order: PaperOrder, qty: Decimal) -> float:
        rules = self.rules.get(order.ref.instrument_id)
        if rules is None:
            return 0.0
        rate, exponent, share = rules.fee_rate, rules.fee_exponent, rules.rebate_rate
        if rate is None or exponent is None or share is None:
            return 0.0
        p = float(order.price)
        return share * rate * math.pow(p * (1 - p), exponent) * float(qty)


def _level_size(levels: Sequence[BookLevel], price: Decimal) -> Decimal:
    return next((lvl.size for lvl in levels if lvl.price == price), ZERO)


def _crossing_size(book: BookSnapshot, order: PaperOrder) -> Decimal:
    """Opposite-side size at or through our price: it would have traded with us."""
    if order.side is Side.BUY:
        return sum((lvl.size for lvl in book.asks if lvl.price <= order.price), ZERO)
    return sum((lvl.size for lvl in book.bids if lvl.price >= order.price), ZERO)

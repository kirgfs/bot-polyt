"""Stage-0 quoting (docs/architecture.md §4.0, §5): fair value from Polymarket's own book.

Pure logic over venue-neutral types (`polybot.venues.base`): the same code runs in paper
mode now and, after an explicit decision, against the exchange. Prices and sizes at the
boundary are Decimal on the market's tick grid; the math in between is float.

- Fair value: EWMA of the microprice. A move of the microprice away from the EWMA by more
  than `jump_ticks` is a jump: quotes come off until the EWMA catches up (§4.0).
- Quotes: reservation price skewed by inventory, half-spread inside the rewards band when
  the market has rewards (`δ ≤ v`, §5), bid rounded down, ask up, never at or through the
  opposite best price (post-only), inside [tick, 1 − tick].
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal

from polybot.venues.base import BookSnapshot, Side

SIZE_STEP = Decimal("0.01")  # order sizes round down to 0.01 shares (docs/api_notes.md §4)
ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class QuoteParams:
    ewma_halflife_s: float
    jump_ticks: int
    jump_cooldown_s: float
    max_book_spread: float
    rewards_spread_fraction: float
    default_half_spread_ticks: int
    min_half_spread_ticks: int
    requote_ticks: int
    skew_ticks: int


@dataclass(frozen=True, slots=True)
class RewardParams:
    """Liquidity rewards of one market in price units (docs/api_notes.md §9)."""

    max_spread: float  # v
    min_size: float  # shares per order to count
    daily_rate: float  # $ per day, whole market


@dataclass(frozen=True, slots=True)
class Quote:
    side: Side
    price: Decimal
    size: Decimal


def best_prices(book: BookSnapshot) -> tuple[Decimal | None, Decimal | None]:
    bid = book.bids[0].price if book.bids else None
    ask = book.asks[0].price if book.asks else None
    return bid, ask


def microprice(book: BookSnapshot) -> float | None:
    """Mid weighted by the opposite sizes at the best levels; None for a one-sided book."""
    if not book.bids or not book.asks:
        return None
    bid, ask = book.bids[0], book.asks[0]
    bid_size, ask_size = float(bid.size), float(ask.size)
    if bid_size + ask_size <= 0:
        return (float(bid.price) + float(ask.price)) / 2
    return (float(bid.price) * ask_size + float(ask.price) * bid_size) / (bid_size + ask_size)


class FairValue:
    """Time-weighted EWMA of the microprice (docs/architecture.md §4.0)."""

    def __init__(self, halflife_s: float) -> None:
        self._halflife_ns = max(halflife_s, 0.0) * 1e9
        self.value: float | None = None
        self.micro: float | None = None
        self._ts_ns = 0

    def update(self, book: BookSnapshot) -> float | None:
        micro = microprice(book)
        self.micro = micro
        if micro is None:
            return self.value
        if self.value is None or self._halflife_ns == 0:
            self.value = micro
        else:
            dt = max(0, book.ts_recv_ns - self._ts_ns)
            alpha = 1.0 - math.exp(-math.log(2) * dt / self._halflife_ns)
            self.value += alpha * (micro - self.value)
        self._ts_ns = book.ts_recv_ns
        return self.value

    def jumped(self, tick: float, jump_ticks: int) -> bool:
        """The book moved away from the fair value faster than it can follow."""
        if self.value is None or self.micro is None:
            return False
        return abs(self.micro - self.value) > jump_ticks * tick + 1e-12


def floor_tick(value: float, tick: Decimal) -> Decimal:
    return (Decimal(str(value)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def ceil_tick(value: float, tick: Decimal) -> Decimal:
    return (Decimal(str(value)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def round_size(size: Decimal) -> Decimal:
    return size.quantize(SIZE_STEP, rounding=ROUND_DOWN)


def half_spread(tick: Decimal, rewards: RewardParams | None, params: QuoteParams) -> float:
    """Inside the rewards band when there is one (δ ≤ v), else a fixed number of ticks."""
    t = float(tick)
    floor_half = params.min_half_spread_ticks * t
    if rewards is None or rewards.max_spread <= 0:
        return max(params.default_half_spread_ticks * t, floor_half)
    target = max(params.rewards_spread_fraction * rewards.max_spread, floor_half)
    return min(target, max(rewards.max_spread, floor_half))


def compute_quotes(
    *,
    book: BookSnapshot,
    fair: float | None,
    tick: Decimal,
    rewards: RewardParams | None,
    position: Decimal,
    max_position: Decimal,
    bid_size: Decimal,
    ask_size: Decimal,
    params: QuoteParams,
) -> list[Quote]:
    """Desired post-only quotes for one outcome token; empty when we must not quote.

    `position` is the net holding of this token (YES − NO for a binary market);
    the side that would push it past ±`max_position` is not quoted.
    """
    best_bid, best_ask = best_prices(book)
    if fair is None or best_bid is None or best_ask is None or best_bid >= best_ask:
        return []
    if float(best_ask - best_bid) > params.max_book_spread:
        return []
    t = float(tick)
    skew = 0.0
    if max_position > 0:
        skew = max(-1.0, min(1.0, float(position / max_position)))
    reservation = fair - skew * params.skew_ticks * t
    half = half_spread(tick, rewards, params)
    bid = min(floor_tick(reservation - half, tick), best_ask - tick)
    ask = max(ceil_tick(reservation + half, tick), best_bid + tick)
    low, high = tick, ONE - tick
    quotes: list[Quote] = []
    bid_size, ask_size = round_size(bid_size), round_size(ask_size)
    if position < max_position and bid_size > 0 and low <= bid <= high and bid < ask:
        quotes.append(Quote(Side.BUY, bid, bid_size))
    if position > -max_position and ask_size > 0 and low <= ask <= high and bid < ask:
        quotes.append(Quote(Side.SELL, ask, ask_size))
    return quotes


def order_size(
    *,
    cost_per_share: Decimal,
    min_order_size: Decimal,
    rewards_min_size: float | None,
    max_order_usd: float,
) -> Decimal:
    """Shares for one order: the rewards minimum when affordable, else the exchange minimum.

    Zero when even the exchange minimum does not fit `max_order_usd`.
    """
    if cost_per_share <= 0:
        return ZERO
    cap = round_size(Decimal(str(max_order_usd)) / cost_per_share)
    wanted = min_order_size
    if rewards_min_size is not None:
        wanted = max(wanted, Decimal(str(rewards_min_size)))
    for size in (round_size(wanted), round_size(min_order_size)):
        if ZERO < size <= cap and size >= min_order_size:
            return size
    return ZERO


def needs_requote(
    current: Quote | None, desired: Quote | None, tick: Decimal, requote_ticks: int
) -> bool:
    """Keep the queue position unless the quote moved by `requote_ticks` or its size changed."""
    if current is None or desired is None:
        return current != desired
    if current.side != desired.side or current.size != desired.size:
        return True
    return abs(current.price - desired.price) >= requote_ticks * tick

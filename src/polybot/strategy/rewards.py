"""Liquidity-reward scoring for estimates in paper mode (docs/api_notes.md §9).

Per the published formula: an order at distance s from the (size-adjusted) mid scores
`((v − s) / v)² · size` when s < v; bids of YES add to Q_one, asks of YES to Q_two; the
market's minute share is `Q_min / ΣQ_min` over makers. We see only aggregated L2 levels, so
the competition is scored as if the whole visible book were one maker: that overstates it
(a sum of per-maker minima is at most the minimum of sums), which makes our share, and the
reward estimate built on it, a lower bound.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from polybot.venues.base import BookSnapshot

# One side alone earns up to 1/c of its weight while the mid is in [0.10, 0.90].
SINGLE_SIDE_DIVISOR = 3.0
MID_BAND = (0.10, 0.90)


def order_score(max_spread: float, distance: float, size: float) -> float:
    if max_spread <= 0 or distance >= max_spread or size <= 0:
        return 0.0
    return ((max_spread - distance) / max_spread) ** 2 * size


def q_min(q_one: float, q_two: float, mid: float) -> float:
    if MID_BAND[0] <= mid <= MID_BAND[1]:
        return max(min(q_one, q_two), max(q_one, q_two) / SINGLE_SIDE_DIVISOR)
    return min(q_one, q_two)


def adjusted_mid(book: BookSnapshot, min_size: float) -> float | None:
    """Mid of the best levels holding at least `min_size`; the plain mid as a fallback."""
    if not book.bids or not book.asks:
        return None
    bid = next((lvl.price for lvl in book.bids if float(lvl.size) >= min_size), book.bids[0].price)
    ask = next((lvl.price for lvl in book.asks if float(lvl.size) >= min_size), book.asks[0].price)
    return (float(bid) + float(ask)) / 2


def side_score(levels: Iterable[tuple[float, float]], mid: float, max_spread: float) -> float:
    return sum(order_score(max_spread, abs(price - mid), size) for price, size in levels)


@dataclass(frozen=True, slots=True)
class RewardSample:
    ours: float
    competition: float

    @property
    def share(self) -> float:
        total = self.ours + self.competition
        return self.ours / total if total > 0 else 0.0


def sample(
    book: BookSnapshot,
    our_bids: Iterable[tuple[float, float]],
    our_asks: Iterable[tuple[float, float]],
    *,
    max_spread: float,
    min_size: float,
) -> RewardSample | None:
    """Our Q_min against the visible book's Q_min for one minute; None without a two-sided book."""
    mid = adjusted_mid(book, min_size)
    if mid is None:
        return None
    eligible_bids = [(p, s) for p, s in our_bids if s >= min_size]
    eligible_asks = [(p, s) for p, s in our_asks if s >= min_size]
    ours = q_min(
        side_score(eligible_bids, mid, max_spread),
        side_score(eligible_asks, mid, max_spread),
        mid,
    )
    competition = q_min(
        side_score(((float(lvl.price), float(lvl.size)) for lvl in book.bids), mid, max_spread),
        side_score(((float(lvl.price), float(lvl.size)) for lvl in book.asks), mid, max_spread),
        mid,
    )
    return RewardSample(ours, competition)

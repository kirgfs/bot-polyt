"""Risk limits of the stage-0 mini-bot (docs/architecture.md §7, §13)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from polybot.venues.base import Side


@dataclass
class DailyLossGuard:
    """Trips when the portfolio value falls `limit_usd` below its value at the UTC day start."""

    limit_usd: float
    day: date | None = None
    start_value: float = 0.0
    tripped: bool = False

    def update(self, today: date, value: float) -> bool:
        if self.day != today:
            self.day, self.start_value, self.tripped = today, value, False
        if value - self.start_value < -self.limit_usd:
            self.tripped = True
        return self.tripped


def max_position_shares(max_position_usd: float, price: float) -> Decimal:
    """Position cap in shares for a cap in dollars at the current price (cost of the long side)."""
    price = min(max(price, 0.01), 0.99)
    return Decimal(str(round(max_position_usd / price, 2)))


def cost_per_share(side: Side, price: Decimal, free_long: Decimal, size: Decimal) -> Decimal:
    """Average cash locked per share: a bid pays the price; an ask sells free inventory
    first and covers the rest like a NO bid at (1 − price) (docs/architecture.md §5)."""
    if side is Side.BUY:
        return price
    if size <= 0:
        return Decimal(1) - price
    covered = min(max(free_long, Decimal(0)), size)
    return (Decimal(1) - price) * (size - covered) / size

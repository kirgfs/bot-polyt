"""Venue-neutral contract between adapters and strategy (docs/architecture.md §12).

Strategy, pricing and risk import only this module, never `venues.polymarket.*`.
A new venue (e.g. `venues/polymarket_us/`, deferred until the scaling phase) adds an
adapter that maps its wire format into these types; the strategy does not change.

M1 defines the contract only: the recorder stores raw venue frames. The Polymarket
adapter implements these protocols in M4, together with the order manager.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class VenueId(StrEnum):
    POLYMARKET = "polymarket"  # international venue, CLOB V2
    POLYMARKET_US = "polymarket_us"  # separate regulated venue and API (later phase)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    venue: VenueId
    market_id: str  # venue market id (Polymarket: condition id)
    instrument_id: str  # tradable outcome (Polymarket: token id)


@dataclass(frozen=True, slots=True)
class MarketRules:
    """Constants the strategy must read from the venue, never from config (CLAUDE.md, rule 3)."""

    tick_size: Decimal
    min_order_size: Decimal
    taker_delay_s: float | None  # Polymarket: secondsDelay
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    resolution_text: str  # rules of resolution, parsed by matching/rules_parser (M2)


@dataclass(frozen=True, slots=True)
class MarketMeta:
    ref: InstrumentRef
    sport: str
    event_key: str  # venue event id
    outcome_label: str
    participants: tuple[str, str]
    scheduled_start_ns: int
    rules: MarketRules


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    ref: InstrumentRef
    bids: Sequence[BookLevel]  # best first
    asks: Sequence[BookLevel]  # best first
    ts_venue_ns: int | None
    ts_recv_ns: int


@dataclass(frozen=True, slots=True)
class TradePrint:
    ref: InstrumentRef
    price: Decimal
    size: Decimal
    aggressor: Side | None
    ts_venue_ns: int | None
    ts_recv_ns: int


class MarketDataVenue(Protocol):
    venue: VenueId

    async def markets(self) -> Sequence[MarketMeta]: ...

    def books(self) -> AsyncIterator[BookSnapshot]: ...

    def trades(self) -> AsyncIterator[TradePrint]: ...


class ExecutionVenue(Protocol):
    """Order entry. Implementations check the live-trading gate (core.config) themselves."""

    venue: VenueId

    async def place_post_only(
        self, ref: InstrumentRef, side: Side, price: Decimal, size: Decimal, expires_ns: int | None
    ) -> str: ...

    async def cancel(self, order_ids: Sequence[str]) -> None: ...

    async def cancel_market(self, market_id: str) -> None: ...

    async def cancel_all(self) -> None: ...

"""Mini-bot bookkeeping shared by the engine and the reports: phases, markets, day stats."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Phase(StrEnum):
    WAITING = "waiting"  # no usable book (yet): no quotes
    QUOTING = "quoting"
    PAUSED = "paused"  # the book jumped: quotes off for a cooldown
    PULLED = "pulled"  # kickoff is near (or the market was dropped): off for good
    HALTED = "halted"  # daily loss limit: off until the next UTC day


@dataclass
class Known:
    """What reports and settlement need about a market we quoted; survives restarts."""

    token: str
    market_id: str
    condition_id: str
    yes_index: int
    start_ns: int
    title: str
    label: str
    league: str
    reviewed: bool


@dataclass
class MarketDay:
    title: str
    label: str
    league: str = ""
    fills: int = 0
    volume_usd: float = 0.0
    rewards_usd: float = 0.0
    quoted_min: int = 0  # minutes with both sides live in the book


@dataclass
class Settlement:
    title: str
    label: str
    outcome: str  # "yes", "no" or the payout of a split result, e.g. "0.5"
    pnl: float


@dataclass
class DayStats:
    """One UTC day of paper trading."""

    day: str
    start_value: float
    rebates_at_start: float = 0.0
    fills: int = 0
    volume_usd: float = 0.0
    rewards_usd: float = 0.0
    settled_pnl: float = 0.0
    settlements: list[Settlement] = field(default_factory=list)
    halted: bool = False
    markets: dict[str, MarketDay] = field(default_factory=dict)

    def market(self, known: Known) -> MarketDay:
        day = self.markets.get(known.token)
        if day is None:
            day = self.markets[known.token] = MarketDay(known.title, known.label, known.league)
        return day

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayStats:
        return cls(
            day=str(data["day"]),
            start_value=float(data["start_value"]),
            rebates_at_start=float(data.get("rebates_at_start", 0.0)),
            fills=int(data.get("fills", 0)),
            volume_usd=float(data.get("volume_usd", 0.0)),
            rewards_usd=float(data.get("rewards_usd", 0.0)),
            settled_pnl=float(data.get("settled_pnl", 0.0)),
            settlements=[Settlement(**s) for s in data.get("settlements", [])],
            halted=bool(data.get("halted", False)),
            markets={t: MarketDay(**m) for t, m in data.get("markets", {}).items()},
        )


@dataclass
class ClosedDay:
    """A finished day with the portfolio at its end, ready for the daily report."""

    stats: DayStats
    end_value: float
    rebates_total: float
    deposit: float
    unreviewed: int  # quoted markets whose rules template nobody approved


@dataclass
class MarketStatus:
    token: str
    title: str
    label: str
    league: str
    start_ns: int
    phase: str
    reason: str
    fair: float | None
    bid: str | None
    ask: str | None
    position: str  # net shares of the Yes token (Decimal as text)
    rewards_daily: float | None
    reviewed: bool


@dataclass
class Status:
    """Snapshot for the status file (Docker healthcheck), the log and Telegram."""

    ts_ns: int
    day: str
    deposit: float
    value: float
    day_start_value: float
    cash: float
    locked: float
    rebates_total: float
    rewards_day: float
    fills_day: int
    volume_day: float
    halted: bool
    unsettled: int  # markets past kickoff with a position waiting for settlement
    markets: list[MarketStatus] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # memory, market_ws, sink

    @property
    def pnl_total(self) -> float:
        return self.value - self.deposit

    @property
    def pnl_day(self) -> float:
        return self.value - self.day_start_value

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra")
        return {"mode": "paper", **data, **extra}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Status:
        names = {f for f in cls.__dataclass_fields__ if f not in ("markets", "extra")}
        fields = {k: v for k, v in data.items() if k in names}
        markets = [MarketStatus(**m) for m in data.get("markets", [])]
        return cls(**fields, markets=markets)

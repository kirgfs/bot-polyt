"""Parse raw Gamma events into typed sports events and markets (docs/api_notes.md §11).

Pure functions over raw JSON. Anything that fails to parse is reported, not guessed:
a market without a parseable start time or token ids is not recordable, let alone
quotable.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from polybot.core.config import SportName
from polybot.core.timeutil import parse_ts_ns


@dataclass(frozen=True, slots=True)
class PmMarket:
    market_id: str
    condition_id: str
    question: str
    slug: str
    sports_market_type: str | None
    line: float | None
    outcomes: tuple[str, ...]
    token_ids: tuple[str, ...]
    game_start_ns: int | None
    game_start_raw: str | None
    game_id: str | None
    active: bool | None
    closed: bool | None
    accepting_orders: bool | None
    enable_order_book: bool | None
    neg_risk: bool | None
    tick_size: Decimal | None
    min_order_size: Decimal | None
    seconds_delay: int | None
    has_description: bool

    @property
    def is_binary_with_tokens(self) -> bool:
        return len(self.outcomes) == 2 and len(self.token_ids) == 2 and all(self.token_ids)


@dataclass(frozen=True, slots=True)
class PmEvent:
    event_id: str
    slug: str
    title: str
    sport: SportName
    tag_slugs: tuple[str, ...]
    game_id: str | None
    sportsradar_match_id: str | None
    home_name: str | None
    away_name: str | None
    live: bool | None
    ended: bool | None
    closed: bool | None
    is_doubles: bool
    markets: tuple[PmMarket, ...]

    @property
    def game_start_ns(self) -> int | None:
        """Earliest parseable market start: the conservative choice for the start guard."""
        starts = [m.game_start_ns for m in self.markets if m.game_start_ns is not None]
        return min(starts) if starts else None

    @property
    def is_match(self) -> bool:
        """Per-game event (not a futures/outright): has sports markets with a start time."""
        return any(
            m.sports_market_type is not None and m.game_start_ns is not None for m in self.markets
        )


@dataclass
class ParseIssues:
    bad_json_sequences: int = 0
    bad_game_start: list[str] = field(default_factory=list)
    missing_condition_id: int = 0
    not_binary: int = 0

    def merge(self, other: ParseIssues) -> None:
        self.bad_json_sequences += other.bad_json_sequences
        self.bad_game_start.extend(other.bad_game_start)
        self.missing_condition_id += other.missing_condition_id
        self.not_binary += other.not_binary

    def as_dict(self) -> dict[str, object]:
        return {
            "bad_json_sequences": self.bad_json_sequences,
            "bad_game_start": len(self.bad_game_start),
            "bad_game_start_examples": self.bad_game_start[:5],
            "missing_condition_id": self.missing_condition_id,
            "not_binary": self.not_binary,
        }


def parse_string_sequence(value: object) -> tuple[str, ...] | None:
    """Gamma encodes outcomes/clobTokenIds as JSON arrays inside strings; None if invalid."""
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, list | tuple) and all(isinstance(v, str | int) for v in value):
        return tuple(str(v) for v in value)
    return None


def _opt_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _opt_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _opt_decimal(value: object) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _opt_float(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _opt_int(value: object) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def parse_market(raw: dict[str, Any], issues: ParseIssues) -> PmMarket | None:
    outcomes = parse_string_sequence(raw.get("outcomes"))
    token_ids = parse_string_sequence(raw.get("clobTokenIds"))
    if outcomes is None or token_ids is None:
        issues.bad_json_sequences += 1
        return None
    condition_id = _opt_str(raw.get("conditionId"))
    if condition_id is None:
        issues.missing_condition_id += 1
        return None
    game_start_raw = _opt_str(raw.get("gameStartTime"))
    game_start_ns = parse_ts_ns(game_start_raw) if game_start_raw else None
    if game_start_raw and game_start_ns is None:
        issues.bad_game_start.append(game_start_raw)
    market = PmMarket(
        market_id=str(raw.get("id", "")),
        condition_id=condition_id,
        question=str(raw.get("question") or ""),
        slug=str(raw.get("slug") or ""),
        sports_market_type=_opt_str(raw.get("sportsMarketType")),
        line=_opt_float(raw.get("line")),
        outcomes=outcomes,
        token_ids=token_ids,
        game_start_ns=game_start_ns,
        game_start_raw=game_start_raw,
        game_id=_opt_str(raw.get("gameId")),
        active=_opt_bool(raw.get("active")),
        closed=_opt_bool(raw.get("closed")),
        accepting_orders=_opt_bool(raw.get("acceptingOrders")),
        enable_order_book=_opt_bool(raw.get("enableOrderBook")),
        neg_risk=_opt_bool(raw.get("negRisk")),
        tick_size=_opt_decimal(raw.get("orderPriceMinTickSize")),
        min_order_size=_opt_decimal(raw.get("orderMinSize")),
        seconds_delay=_opt_int(raw.get("secondsDelay")),
        has_description=bool(raw.get("description")),
    )
    if not market.is_binary_with_tokens:
        issues.not_binary += 1
    return market


def _is_doubles(raw: dict[str, Any]) -> bool:
    title = str(raw.get("title") or "")
    slug = str(raw.get("slug") or "")
    # Observed in real payloads: "(Doubles)" in the title, "-doubles-" in the slug [CAP].
    return "(doubles)" in title.lower() or "-doubles-" in slug.lower()


def parse_event(raw: dict[str, Any], sport: SportName, issues: ParseIssues) -> PmEvent:
    markets: list[PmMarket] = []
    for raw_market in raw.get("markets") or []:
        if isinstance(raw_market, dict):
            market = parse_market(raw_market, issues)
            if market is not None:
                markets.append(market)
    tags = tuple(
        str(tag.get("slug"))
        for tag in raw.get("tags") or []
        if isinstance(tag, dict) and tag.get("slug")
    )
    return PmEvent(
        event_id=str(raw.get("id", "")),
        slug=str(raw.get("slug") or ""),
        title=str(raw.get("title") or ""),
        sport=sport,
        tag_slugs=tags,
        game_id=_opt_str(raw.get("gameId")),
        sportsradar_match_id=_opt_str(raw.get("sportsradarMatchId")),
        home_name=_opt_str(raw.get("homeTeamName")),
        away_name=_opt_str(raw.get("awayTeamName")),
        live=_opt_bool(raw.get("live")),
        ended=_opt_bool(raw.get("ended")),
        closed=_opt_bool(raw.get("closed")),
        is_doubles=_is_doubles(raw),
        markets=tuple(markets),
    )


def recordable_markets(
    events: Iterable[PmEvent],
    *,
    market_types: dict[SportName, tuple[str, ...]],
    exclude_doubles: dict[SportName, bool],
    now_ns: int,
    horizon_ns: int,
    lookback_ns: int,
) -> list[tuple[PmEvent, PmMarket]]:
    """Markets whose books the recorder subscribes to right now."""
    selected: list[tuple[PmEvent, PmMarket]] = []
    for event in events:
        if event.is_doubles and exclude_doubles.get(event.sport, False):
            continue
        for market in event.markets:
            if market.sports_market_type not in market_types.get(event.sport, ()):
                continue
            if not market.is_binary_with_tokens or market.closed is True:
                continue
            if market.enable_order_book is False:
                continue
            start = market.game_start_ns
            if start is None or not (now_ns - lookback_ns <= start <= now_ns + horizon_ns):
                continue
            selected.append((event, market))
    return selected


def slim_event(event: PmEvent, market_types: Iterable[str]) -> PmEvent:
    """Keep only markets of the given types: side markets dominate the memory of an event.

    An event without such markets keeps its earliest-starting market, so its start time
    stays known (OddsPapi matching, counts).
    """
    types = frozenset(market_types)
    kept = tuple(m for m in event.markets if m.sports_market_type in types)
    if not kept:
        timed = [m for m in event.markets if m.game_start_ns is not None]
        kept = (min(timed, key=lambda m: m.game_start_ns or 0),) if timed else ()
    return dataclasses.replace(event, markets=kept)

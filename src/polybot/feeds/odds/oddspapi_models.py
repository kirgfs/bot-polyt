"""Parsers for OddsPapi REST payloads (docs/data_sources.md §5, all fields [2nd]).

The vendor response shapes come from search-result excerpts of their docs, not from a
spec we could read, so every parser is lenient: unknown wrappers and missing fields
yield fewer items, never exceptions. Raw responses are stored anyway.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from polybot.core.timeutil import parse_ts_ns


@dataclass(frozen=True, slots=True)
class OpFixture:
    fixture_id: str
    sport_id: int | None
    tournament_id: int | None
    tournament_name: str
    category_name: str
    p1_name: str
    p2_name: str
    start_ns: int | None
    status_id: int | None
    has_odds: bool | None
    betradar_id: str | None


@dataclass(frozen=True, slots=True)
class OpPrice:
    fixture_id: str
    bookmaker: str
    market_id: str
    outcome_id: str
    player_key: str
    price: float | None
    active: bool | None
    limit: float | None
    changed_at_ns: int | None
    bookmaker_changed_at_ns: int | None


def _as_list(data: Any, *keys: str) -> list[Any]:
    """Payload as a list: either the list itself or the first list under known wrapper keys."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (*keys, "data", "results", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _opt_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _opt_float(value: object) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def digits_id(value: object) -> str | None:
    """Provider ids appear as numbers or "sr:match:123" strings; compare by digits only."""
    if value is None or isinstance(value, bool):
        return None
    text = "".join(ch for ch in str(value) if ch.isdigit())
    return text.lstrip("0") or None


def parse_fixture(raw: dict[str, Any]) -> OpFixture | None:
    fixture_id = raw.get("fixtureId")
    if fixture_id in (None, ""):
        return None
    providers = raw.get("externalProviders")
    betradar = providers.get("betradarId") if isinstance(providers, dict) else None
    has_odds = raw.get("hasOdds")
    return OpFixture(
        fixture_id=str(fixture_id),
        sport_id=_opt_int(raw.get("sportId")),
        tournament_id=_opt_int(raw.get("tournamentId")),
        tournament_name=str(raw.get("tournamentName") or ""),
        category_name=str(raw.get("categoryName") or ""),
        p1_name=str(raw.get("participant1Name") or ""),
        p2_name=str(raw.get("participant2Name") or ""),
        start_ns=parse_ts_ns(raw.get("startTime")),
        status_id=_opt_int(raw.get("statusId")),
        has_odds=has_odds if isinstance(has_odds, bool) else None,
        betradar_id=digits_id(betradar),
    )


def parse_fixtures(data: Any) -> list[OpFixture]:
    fixtures = []
    for raw in _as_list(data, "fixtures"):
        if isinstance(raw, dict) and (fixture := parse_fixture(raw)) is not None:
            fixtures.append(fixture)
    return fixtures


def iter_prices(odds: Any) -> Iterator[OpPrice]:
    """Walk bookmakerOdds.{slug}.markets.{id}.outcomes.{id}.players.{key} of one fixture."""
    if not isinstance(odds, dict):
        return
    fixture_id = str(odds.get("fixtureId") or "")
    bookmakers = odds.get("bookmakerOdds")
    if not isinstance(bookmakers, dict):
        return
    for slug, book in bookmakers.items():
        markets = book.get("markets") if isinstance(book, dict) else None
        if not isinstance(markets, dict):
            continue
        for market_id, market in markets.items():
            outcomes = market.get("outcomes") if isinstance(market, dict) else None
            if not isinstance(outcomes, dict):
                continue
            for outcome_id, outcome in outcomes.items():
                players = outcome.get("players") if isinstance(outcome, dict) else None
                if not isinstance(players, dict):
                    continue
                for player_key, quote in players.items():
                    if not isinstance(quote, dict):
                        continue
                    active = quote.get("active")
                    yield OpPrice(
                        fixture_id=fixture_id,
                        bookmaker=str(slug),
                        market_id=str(market_id),
                        outcome_id=str(outcome_id),
                        player_key=str(player_key),
                        price=_opt_float(quote.get("price")),
                        active=active if isinstance(active, bool) else None,
                        limit=_opt_float(quote.get("limit")),
                        changed_at_ns=parse_ts_ns(quote.get("changedAt")),
                        bookmaker_changed_at_ns=parse_ts_ns(quote.get("bookmakerChangedAt")),
                    )


def iter_odds_objects(data: Any) -> Iterator[dict[str, Any]]:
    """`/odds` returns one fixture object; `/odds-by-tournaments` returns a list of them."""
    if isinstance(data, dict) and "bookmakerOdds" in data:
        yield data
        return
    for item in _as_list(data, "fixtures"):
        if isinstance(item, dict):
            yield item

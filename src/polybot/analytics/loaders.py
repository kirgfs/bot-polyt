"""Load typed objects back from the raw Parquet store."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import duckdb

from polybot.core.config import SportName
from polybot.data.records import Source
from polybot.data.store import iter_query, query, scan
from polybot.feeds.odds.oddspapi_models import OpFixture, iter_odds_objects, parse_fixtures
from polybot.venues.polymarket.markets import ParseIssues, PmEvent, parse_event, slim_event

_SPORTS: tuple[SportName, ...] = ("tennis", "soccer", "basketball")


def load_pm_events(
    con: duckdb.DuckDBPyConnection,
    root: Path,
    *,
    since_ns: int = 0,
    until_ns: int | None = None,
    dates: Sequence[str] | None = None,
    market_types: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[PmEvent], ParseIssues]:
    """Latest recorded state of every Gamma event (one row per event id).

    Rows are streamed and parsed one by one; with `market_types` (sport → types) each
    event keeps only those markets, so a day of full snapshots fits a small container.
    """
    issues = ParseIssues()
    table = scan(root, Source.GAMMA_EVENTS, dates=dates)
    if table is None:
        return [], issues
    until = until_ns if until_ns is not None else 2**63 - 1
    # Latest timestamp per event first (no payloads), then only those payloads: a window
    # over payloads would hold every snapshot of the period in memory at once.
    rows = iter_query(
        con,
        f"""
        WITH latest AS (
            SELECT key, max(ts_recv_ns) AS ts FROM {table}
            WHERE kind = 'rest' AND ts_recv_ns >= ? AND ts_recv_ns < ?
            GROUP BY key
        )
        SELECT t.key, t.event_type, t.payload
        FROM {table} t JOIN latest l ON t.key = l.key AND t.ts_recv_ns = l.ts
        WHERE t.kind = 'rest'
        """,
        [since_ns, until],
    )
    events: dict[str, PmEvent] = {}
    for key, sport, payload in rows:
        if sport not in _SPORTS or key in events:
            continue
        event = parse_event(json.loads(payload), cast(SportName, sport), issues)
        if market_types is not None:
            event = slim_event(event, market_types.get(sport, ()))
        events[key] = event
    return list(events.values()), issues


def _endpoint_has(param: str, value: object) -> str:
    return f"{re.escape(param)}={re.escape(str(value))}(&|$)"


def load_latest_fixtures(
    con: duckdb.DuckDBPyConnection, root: Path, sport_id: int
) -> list[OpFixture]:
    """All fixtures of a sport seen in /fixtures responses; the latest copy of each wins."""
    table = scan(root, Source.ODDSPAPI_REST)
    if table is None:
        return []
    rows = iter_query(
        con,
        f"""
        SELECT payload FROM {table}
        WHERE kind = 'rest' AND event_type = 'fixtures' AND status = 200
          AND regexp_matches(endpoint, ?)
        ORDER BY ts_recv_ns
        """,
        [_endpoint_has("sportId", sport_id)],
    )
    latest: dict[str, OpFixture] = {}
    for (payload,) in rows:
        for fixture in parse_fixtures(json.loads(payload)):
            latest[fixture.fixture_id] = fixture
    return list(latest.values())


def load_tournaments(
    con: duckdb.DuckDBPyConnection, root: Path, sport_id: int
) -> dict[int, tuple[str, str]]:
    """tournamentId → (tournamentName, categoryName) from the latest /tournaments response."""
    table = scan(root, Source.ODDSPAPI_REST)
    if table is None:
        return {}
    rows = query(
        con,
        f"""
        SELECT payload FROM {table}
        WHERE kind = 'rest' AND event_type = 'tournaments' AND status = 200
          AND regexp_matches(endpoint, ?)
        ORDER BY ts_recv_ns DESC LIMIT 1
        """,
        [_endpoint_has("sportId", sport_id)],
    )
    result: dict[int, tuple[str, str]] = {}
    for (payload,) in rows:
        data = json.loads(payload)
        items = (
            data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
        )
        for item in items or []:
            if isinstance(item, dict) and item.get("tournamentId") is not None:
                try:
                    tid = int(str(item["tournamentId"]))
                except ValueError:
                    continue
                result[tid] = (
                    str(item.get("tournamentName") or ""),
                    str(item.get("categoryName") or ""),
                )
    return result


def iter_recorded_odds(
    con: duckdb.DuckDBPyConnection, root: Path
) -> Iterator[tuple[int, dict[str, Any]]]:
    """(ts_recv_ns, fixture odds object) from every /odds and /odds-by-tournaments response."""
    table = scan(root, Source.ODDSPAPI_REST)
    if table is None:
        return
    rows = iter_query(
        con,
        f"""
        SELECT ts_recv_ns, payload FROM {table}
        WHERE kind = 'rest' AND status = 200
          AND event_type IN ('odds', 'odds-by-tournaments')
        ORDER BY ts_recv_ns
        """,
    )
    for ts, payload in rows:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for obj in iter_odds_objects(data):
            yield int(ts), obj

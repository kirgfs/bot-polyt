"""Load typed objects back from the raw Parquet store."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import duckdb

from polybot.core.config import SportName
from polybot.data.records import Source
from polybot.data.store import query, scan
from polybot.feeds.odds.oddspapi_models import OpFixture, iter_odds_objects, parse_fixtures
from polybot.venues.polymarket.markets import ParseIssues, PmEvent, parse_event

_SPORTS: tuple[SportName, ...] = ("tennis", "soccer", "basketball")


def load_pm_events(
    con: duckdb.DuckDBPyConnection, root: Path, *, since_ns: int = 0
) -> tuple[list[PmEvent], ParseIssues]:
    """Latest recorded state of every Gamma event (one row per event id)."""
    issues = ParseIssues()
    table = scan(root, Source.GAMMA_EVENTS)
    if table is None:
        return [], issues
    rows = query(
        con,
        f"""
        SELECT event_type, payload FROM (
            SELECT event_type, payload,
                   row_number() OVER (PARTITION BY key ORDER BY ts_recv_ns DESC) AS rn
            FROM {table}
            WHERE kind = 'rest' AND ts_recv_ns >= ?
        ) WHERE rn = 1
        """,
        [since_ns],
    )
    events = []
    for sport, payload in rows:
        if sport in _SPORTS:
            events.append(parse_event(json.loads(payload), cast(SportName, sport), issues))
    return events, issues


def _endpoint_has(param: str, value: object) -> str:
    return f"{re.escape(param)}={re.escape(str(value))}(&|$)"


def load_latest_fixtures(
    con: duckdb.DuckDBPyConnection, root: Path, sport_id: int
) -> list[OpFixture]:
    """All fixtures of a sport seen in /fixtures responses; the latest copy of each wins."""
    table = scan(root, Source.ODDSPAPI_REST)
    if table is None:
        return []
    rows = query(
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
    rows = query(
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

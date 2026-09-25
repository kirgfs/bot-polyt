"""OddsPapi quality metrics for docs/data_sources.md §6: coverage, sharp prices, latency, matching.

Pure functions over typed objects; the eval tool and the M1 report both use them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polybot.analytics.loaders import (
    iter_recorded_odds,
    load_latest_fixtures,
    load_pm_events,
    load_tournaments,
)
from polybot.analytics.stats import Summary, md_table, summarize
from polybot.core.config import AppConfig, RecorderConfig, SportName
from polybot.core.timeutil import NS_PER_MS
from polybot.data.store import connect
from polybot.feeds.odds.oddspapi_models import OpFixture, iter_prices
from polybot.matching.event_matcher import MatcherConfig, MatchResult, Method, match_event
from polybot.matching.tournaments import best_tournament, tennis_level, title_tournament
from polybot.venues.polymarket.markets import PmEvent

SHARP_BOOKS = ("pinnacle", "betfair", "singbet", "sbobet", "matchbook", "smarkets")


def recordable_events(events: Iterable[PmEvent], cfg: RecorderConfig) -> list[PmEvent]:
    """Match events we would quote: configured market types, singles only where configured."""
    selected = []
    for event in events:
        sport_cfg = cfg.sports.get(event.sport)
        if sport_cfg is None or not event.is_match:
            continue
        if event.is_doubles and sport_cfg.exclude_doubles:
            continue
        if any(m.sports_market_type in sport_cfg.market_types for m in event.markets):
            selected.append(event)
    return selected


def event_level(
    event: PmEvent, fixture: OpFixture | None, tournaments: dict[int, tuple[str, str]]
) -> str:
    if event.sport != "tennis":
        return "all"
    if fixture is not None:
        name, category = tournaments.get(fixture.tournament_id or -1, ("", ""))
        return tennis_level(name, category, fixture.tournament_name, fixture.category_name)
    tid = best_tournament(
        title_tournament(event.title), {tid: name for tid, (name, _) in tournaments.items()}
    )
    return tennis_level(*tournaments[tid]) if tid is not None else "unknown"


def winner_prices(
    odds_objects: Iterable[tuple[int, dict[str, Any]]],
    fixtures: dict[str, OpFixture],
    winner_market_by_sport_id: dict[int, int | None],
) -> dict[str, set[str]]:
    """fixture id → bookmakers with an active match-winner price."""
    found: dict[str, set[str]] = defaultdict(set)
    for _, obj in odds_objects:
        for price in iter_prices(obj):
            fixture = fixtures.get(price.fixture_id)
            if fixture is None or fixture.sport_id is None:
                continue
            winner = winner_market_by_sport_id.get(fixture.sport_id)
            if winner is None or price.market_id != str(winner):
                continue
            if price.price is not None and price.active is not False:
                found[price.fixture_id].add(price.bookmaker)
    return found


@dataclass
class CoverageCell:
    pm_events: int = 0
    by_method: Counter[str] = field(default_factory=Counter)
    with_book: Counter[str] = field(default_factory=Counter)


@dataclass
class MatchValidation:
    """Fuzzy matcher checked against exact Betradar-id matches (ground truth)."""

    agree: int = 0
    wrong: int = 0
    refused: int = 0

    @property
    def precision(self) -> float | None:
        accepted = self.agree + self.wrong
        return self.agree / accepted if accepted else None


@dataclass
class CoverageResult:
    cells: dict[tuple[str, str], CoverageCell]
    matches: list[tuple[PmEvent, MatchResult, str]]
    validation: MatchValidation


def coverage(
    events: Iterable[PmEvent],
    *,
    fixtures_by_sport: dict[SportName, list[OpFixture]],
    tournaments_by_sport: dict[SportName, dict[int, tuple[str, str]]],
    sport_ids: dict[SportName, int],
    prices: dict[str, set[str]],
    cfg: MatcherConfig | None = None,
) -> CoverageResult:
    cfg = cfg or MatcherConfig()
    cells: dict[tuple[str, str], CoverageCell] = defaultdict(CoverageCell)
    matches: list[tuple[PmEvent, MatchResult, str]] = []
    validation = MatchValidation()
    for event in events:
        fixtures = fixtures_by_sport.get(event.sport, [])
        by_id = {f.fixture_id: f for f in fixtures}
        sport_id = sport_ids.get(event.sport)
        result = match_event(event, fixtures, sport_id=sport_id, cfg=cfg)
        fixture = by_id.get(result.fixture_id or "")
        level = event_level(event, fixture, tournaments_by_sport.get(event.sport, {}))
        cell = cells[(event.sport, level)]
        cell.pm_events += 1
        cell.by_method[result.method.value] += 1
        for book in prices.get(result.fixture_id or "", ()):
            cell.with_book[book] += 1
        matches.append((event, result, level))
        if result.method is Method.BETRADAR_ID:
            fuzzy = match_event(event, fixtures, sport_id=sport_id, cfg=cfg, use_ids=False)
            if fuzzy.fixture_id is None:
                validation.refused += 1
            elif fuzzy.fixture_id == result.fixture_id:
                validation.agree += 1
            else:
                validation.wrong += 1
    return CoverageResult(dict(cells), matches, validation)


def render_coverage(result: CoverageResult, books: Iterable[str] = ("pinnacle",)) -> str:
    books = tuple(books)
    header = [
        "вид",
        "уровень",
        "рынков PM",
        "id",
        "fuzzy",
        "неоднозначно",
        "нет",
        "доля сопоставлено",
    ]
    header += [f"с ценой {b}" for b in books]
    rows = []
    for (sport, level), cell in sorted(result.cells.items()):
        matched = cell.by_method[Method.BETRADAR_ID.value] + cell.by_method[Method.FUZZY.value]
        share = f"{matched / cell.pm_events:.0%}" if cell.pm_events else "—"
        row: list[object] = [
            sport,
            level,
            cell.pm_events,
            cell.by_method[Method.BETRADAR_ID.value],
            cell.by_method[Method.FUZZY.value],
            cell.by_method[Method.AMBIGUOUS.value],
            cell.by_method[Method.NONE.value],
            share,
        ]
        for book in books:
            count = sum(v for k, v in cell.with_book.items() if book in k)
            row.append(f"{count} ({count / matched:.0%})" if matched else "—")
        rows.append(row)
    v = result.validation
    precision = "—" if v.precision is None else f"{v.precision:.1%}"
    return (
        md_table(header, rows)
        + f"\n\nПроверка fuzzy по Betradar id: совпало {v.agree}, **ошибочно {v.wrong}**, "
        f"отказ {v.refused}; точность среди принятых — {precision}."
    )


@dataclass
class LatencyStats:
    ingest_ms: Summary
    e2e_ms: Summary
    update_interval_s: Summary
    fresh_changes: int


def odds_latency(
    odds_objects: Iterable[tuple[int, dict[str, Any]]], bookmaker: str = "pinnacle"
) -> LatencyStats:
    """Latency of price changes seen for the first time (docs/data_sources.md §4.1).

    ingest = changedAt − bookmakerChangedAt; e2e = receive time − bookmakerChangedAt.
    The first observation of a price is skipped: its freshness is unknown.
    """
    last_changed: dict[tuple[str, str, str, str], int] = {}
    last_bookmaker_change: dict[tuple[str, str, str, str], int] = {}
    ingest: list[float] = []
    e2e: list[float] = []
    intervals: list[float] = []
    fresh = 0
    for ts_recv, obj in odds_objects:
        for price in iter_prices(obj):
            if bookmaker not in price.bookmaker or price.changed_at_ns is None:
                continue
            key = (price.fixture_id, price.market_id, price.outcome_id, price.player_key)
            previous = last_changed.get(key)
            last_changed[key] = max(previous or 0, price.changed_at_ns)
            if previous is None or price.changed_at_ns <= previous:
                continue
            fresh += 1
            source_ts = price.bookmaker_changed_at_ns
            if source_ts is not None:
                ingest.append((price.changed_at_ns - source_ts) / NS_PER_MS)
                e2e.append((ts_recv - source_ts) / NS_PER_MS)
                prev_source = last_bookmaker_change.get(key)
                if prev_source is not None and source_ts > prev_source:
                    intervals.append((source_ts - prev_source) / 1e9)
                last_bookmaker_change[key] = source_ts
            else:
                e2e.append((ts_recv - price.changed_at_ns) / NS_PER_MS)
    return LatencyStats(summarize(ingest), summarize(e2e), summarize(intervals), fresh)


def render_latency(stats: LatencyStats, bookmaker: str = "pinnacle") -> str:
    header = ["метрика", "n", "p50", "p95", "p99", "min", "max"]
    rows = [
        [f"{bookmaker}: changedAt − bookmakerChangedAt, мс", *stats.ingest_ms.row("{:.0f}")],
        [f"{bookmaker}: получение − bookmakerChangedAt, мс", *stats.e2e_ms.row("{:.0f}")],
        [
            f"{bookmaker}: интервал между изменениями цены, с",
            *stats.update_interval_s.row("{:.1f}"),
        ],
    ]
    return md_table(header, rows) + f"\n\nНовых изменений цены: {stats.fresh_changes}."


def summary_tables(
    cfg: AppConfig,
    raw_root: Path,
    *,
    events: list[PmEvent] | None = None,
    books: tuple[str, ...] = ("pinnacle", "betfair", "singbet", "sbobet"),
) -> str:
    """Offline §6 table parts from the store: coverage, sharp prices, matching, latency."""
    con = connect()
    rec = cfg.recorder
    sport_ids = {s: i for s, i in rec.oddspapi.sport_ids.items() if s in rec.sports}
    if events is None:
        recorded, _ = load_pm_events(con, raw_root)
        events = recordable_events(recorded, rec)
    fixtures = {s: load_latest_fixtures(con, raw_root, sid) for s, sid in sport_ids.items()}
    tournaments = {s: load_tournaments(con, raw_root, sid) for s, sid in sport_ids.items()}
    by_id = {f.fixture_id: f for fs in fixtures.values() for f in fs}
    winner = {sid: rec.oddspapi.winner_market_ids.get(s) for s, sid in sport_ids.items()}
    prices = winner_prices(iter_recorded_odds(con, raw_root), by_id, winner)
    result = coverage(
        events,
        fixtures_by_sport=fixtures,
        tournaments_by_sport=tournaments,
        sport_ids=sport_ids,
        prices=prices,
    )
    latency = odds_latency(iter_recorded_odds(con, raw_root), "pinnacle")
    return (
        "#### Покрытие и цены острых букмекеров\n\n"
        + render_coverage(result, books)
        + "\n\n#### Задержка (Pinnacle)\n\n"
        + render_latency(latency)
    )

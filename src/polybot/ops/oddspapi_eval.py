"""OddsPapi evaluation on the free/trial tier: plan and table in docs/data_sources.md §6.

Every step spends a few requests (the persistent budget is shared with the recorder),
stores the raw responses in Parquet and prints its part of the result table. `summary`
is offline: it rebuilds the whole table from what is already recorded.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polybot.analytics.loaders import (
    iter_recorded_odds,
    load_latest_fixtures,
    load_tournaments,
)
from polybot.analytics.oddspapi_quality import (
    SHARP_BOOKS,
    coverage,
    odds_latency,
    recordable_events,
    render_latency,
    summary_tables,
)
from polybot.analytics.stats import md_table
from polybot.core.config import AppConfig, Settings, SportName
from polybot.core.http import make_client
from polybot.core.timeutil import NS_PER_S, now_ns
from polybot.data.sink import ParquetSink
from polybot.data.store import connect
from polybot.feeds.odds.oddspapi import OddsPapiClient, RequestBudget
from polybot.feeds.odds.oddspapi_models import (
    OpFixture,
    iter_odds_objects,
    iter_prices,
    parse_fixtures,
)
from polybot.matching.event_matcher import MatcherConfig, Method, match_event
from polybot.matching.tournaments import tennis_level
from polybot.recorder.discovery import Discovery
from polybot.recorder.oddspapi_task import iso_utc
from polybot.venues.polymarket.gamma import GammaClient
from polybot.venues.polymarket.markets import PmEvent

TOURNAMENTS_PER_CALL = 10


@dataclass
class EvalContext:
    settings: Settings
    cfg: AppConfig
    sink: ParquetSink
    client: OddsPapiClient
    gamma: GammaClient

    @property
    def raw_root(self) -> Path:
        return self.sink.root

    @property
    def sport_ids(self) -> dict[SportName, int]:
        return {
            s: i
            for s, i in self.cfg.recorder.oddspapi.sport_ids.items()
            if s in self.cfg.recorder.sports
        }

    @property
    def winner_by_sport_id(self) -> dict[int, int | None]:
        winner = self.cfg.recorder.oddspapi.winner_market_ids
        return {sid: winner.get(s) for s, sid in self.sport_ids.items()}


async def _json(ctx: EvalContext, path: str, params: dict[str, Any] | None = None) -> Any:
    response = await ctx.client.get(path, params)
    if response is None:
        raise RuntimeError("request budget exhausted (see data/state/oddspapi_budget.json)")
    if not response.ok:
        return None
    return response.json()


async def step_meta(ctx: EvalContext) -> str:
    """Steps 1–3: sports, bookmakers, tournaments by level, market catalog."""
    out = []
    sports = await _json(ctx, "/sports")
    rows = [
        [s.get("sportId"), s.get("slug") or s.get("sportName")]
        for s in sports or []
        if isinstance(s, dict)
    ]
    out += [
        "### /sports (ожидаем 10 футбол, 11 баскетбол, 12 теннис)",
        md_table(["id", "slug"], rows),
    ]
    books = await _json(ctx, "/bookmakers")
    slugs = sorted(
        str(b.get("slug") or b.get("bookmaker") or b) if isinstance(b, dict) else str(b)
        for b in (books if isinstance(books, list) else (books or {}).get("data", []) or [])
    )
    sharp = [
        s for s in slugs if any(h in s.lower() for h in (*SHARP_BOOKS, "polymarket", "kalshi"))
    ]
    out += ["", f"### /bookmakers: всего {len(slugs)}; острые и биржи: {', '.join(sharp) or '—'}"]
    for sport, sport_id in ctx.sport_ids.items():
        tournaments = await _json(ctx, "/tournaments", {"sportId": sport_id}) or []
        levels: Counter[str] = Counter()
        for t in tournaments if isinstance(tournaments, list) else []:
            if isinstance(t, dict) and (t.get("futureFixtures") or t.get("upcomingFixtures")):
                name, cat = str(t.get("tournamentName") or ""), str(t.get("categoryName") or "")
                levels[tennis_level(name, cat) if sport == "tennis" else cat or "?"] += 1
        out += [
            "",
            f"### {sport}: турниры с будущими матчами",
            md_table(["уровень/категория", "турниров"], levels.most_common(15)),
        ]
        markets = await _json(ctx, "/markets", {"sportId": sport_id}) or []
        winner = ctx.cfg.recorder.oddspapi.winner_market_ids.get(sport)
        hits = [
            [
                m.get("marketId"),
                m.get("marketName") or m.get("name"),
                json.dumps(m.get("outcomes"))[:120],
            ]
            for m in (markets if isinstance(markets, list) else [])
            if isinstance(m, dict)
            and (
                str(m.get("marketId")) == str(winner)
                or any(
                    w in str(m.get("marketName") or m.get("name") or "").lower()
                    for w in ("winner", "moneyline", "1x2")
                )
            )
        ][:12]
        out += [
            "",
            f"### {sport}: рынок победителя (в конфиге: {winner})",
            md_table(["marketId", "name", "outcomes"], hits),
        ]
    return "\n".join(out)


async def step_fixtures(ctx: EvalContext, days: int = 10) -> str:
    """Step 4: fixtures for the next `days` (max 10 without tournamentId)."""
    now = now_ns()
    until = now + days * 86400 * NS_PER_S - 3600 * NS_PER_S
    rows = []
    for sport, sport_id in ctx.sport_ids.items():
        data = await _json(
            ctx, "/fixtures", {"sportId": sport_id, "from": iso_utc(now), "to": iso_utc(until)}
        )
        fixtures = parse_fixtures(data)
        rows.append(
            [
                sport,
                len(fixtures),
                sum(1 for f in fixtures if f.has_odds),
                sum(1 for f in fixtures if f.betradar_id),
            ]
        )
    return md_table(["вид", "матчей", "hasOdds", "с betradarId"], rows)


async def _current_pm_events(ctx: EvalContext) -> list[PmEvent]:
    discovery = Discovery(ctx.gamma, ctx.cfg.recorder, ctx.sink)
    await discovery.resolve_tags()
    result = await discovery.poll()
    return recordable_events(result.events.values(), ctx.cfg.recorder)


def _fixtures_from_store(ctx: EvalContext) -> dict[SportName, list[OpFixture]]:
    con = connect()
    return {s: load_latest_fixtures(con, ctx.raw_root, sid) for s, sid in ctx.sport_ids.items()}


async def step_coverage(ctx: EvalContext, bookmaker: str, max_calls: int) -> str:
    """Step 5: match Polymarket markets to fixtures, then one bookmaker's prices by tournament."""
    events = await _current_pm_events(ctx)
    fixtures = _fixtures_from_store(ctx)
    if not any(fixtures.values()):
        return "Нет записанных /fixtures: сначала `polybot oddspapi-eval fixtures`."
    matcher = MatcherConfig()
    tournament_ids: set[int] = set()
    for event in events:
        pool = fixtures.get(event.sport, [])
        match = match_event(event, pool, sport_id=ctx.sport_ids.get(event.sport), cfg=matcher)
        fixture = next((f for f in pool if f.fixture_id == match.fixture_id), None)
        if fixture is not None and fixture.tournament_id is not None:
            tournament_ids.add(fixture.tournament_id)
    calls = 0
    ordered = sorted(tournament_ids)
    for start in range(0, len(ordered), TOURNAMENTS_PER_CALL):
        if calls >= max_calls:
            break
        batch = ordered[start : start + TOURNAMENTS_PER_CALL]
        await _json(
            ctx,
            "/odds-by-tournaments",
            {"bookmaker": bookmaker, "tournamentIds": ",".join(map(str, batch))},
        )
        calls += 1
    await ctx.sink.flush()
    note = (
        f"Турниров с сопоставленными матчами: {len(ordered)}, "
        f"запросов odds-by-tournaments: {calls}."
    )
    return note + "\n\n" + summary_tables(ctx.cfg, ctx.raw_root, events=events, books=(bookmaker,))


async def step_sample(ctx: EvalContext, per_group: int) -> str:
    """Step 6: /odds for a stratified sample: which bookmakers really price the winner market."""
    events = await _current_pm_events(ctx)
    fixtures = _fixtures_from_store(ctx)
    result = coverage(
        events,
        fixtures_by_sport=fixtures,
        tournaments_by_sport=_tournaments_from_store(ctx),
        sport_ids=ctx.sport_ids,
        prices={},
    )
    chosen: dict[tuple[str, str], list[str]] = {}
    for event, match, level in sorted(result.matches, key=lambda m: m[0].game_start_ns or 0):
        picked = chosen.setdefault((event.sport, level), [])
        if (
            match.fixture_id
            and match.method in (Method.BETRADAR_ID, Method.FUZZY)
            and len(picked) < per_group
        ):
            picked.append(match.fixture_id)
    presence: dict[tuple[str, str], Counter[str]] = {}
    polymarket_example: str | None = None
    for group, fixture_ids in chosen.items():
        counter = presence.setdefault(group, Counter())
        for fixture_id in fixture_ids:
            data = await _json(ctx, "/odds", {"fixtureId": fixture_id})
            for obj in iter_odds_objects(data):
                books = {p.bookmaker for p in iter_prices(obj) if p.price is not None}
                counter.update(books)
                counter["__fixtures__"] += 1
                if polymarket_example is None and isinstance(obj.get("bookmakerOdds"), dict):
                    entry = obj["bookmakerOdds"].get("polymarket")
                    if entry is not None:
                        polymarket_example = json.dumps(entry)[:1500]
    rows = []
    for (sport, level), counter in sorted(presence.items()):
        n = counter.pop("__fixtures__", 0)
        top = ", ".join(
            f"{b} {c}/{n}"
            for b, c in counter.most_common()
            if any(h in b for h in (*SHARP_BOOKS, "polymarket", "kalshi"))
        )
        rows.append([sport, level, n, top or "—", len(counter)])
    text = md_table(
        ["вид", "уровень", "матчей", "острые/биржи (есть цена)", "всего букмекеров"], rows
    )
    if polymarket_example:
        text += (
            "\n\nПример записи `bookmakerOdds.polymarket` (есть ли id рынка Polymarket?):"
            f"\n\n```json\n{polymarket_example}\n```"
        )
    return text


async def step_burst(
    ctx: EvalContext, n_fixtures: int, duration_s: float, interval_s: float, bookmaker: str
) -> str:
    """Step 7: poll /odds for fixtures starting soon to measure update latency."""
    events = await _current_pm_events(ctx)
    fixtures = _fixtures_from_store(ctx)
    now = now_ns()
    soon: list[tuple[int, str]] = []
    for event in events:
        start = event.game_start_ns
        if start is None or not (now + 5 * 60 * NS_PER_S <= start <= now + 60 * 60 * NS_PER_S):
            continue
        pool = fixtures.get(event.sport, [])
        match = match_event(
            event, pool, sport_id=ctx.sport_ids.get(event.sport), cfg=MatcherConfig()
        )
        if match.fixture_id:
            soon.append((start, match.fixture_id))
    targets = [fid for _, fid in sorted(soon)[:n_fixtures]]
    if not targets:
        return "Нет сопоставленных матчей со стартом через 5–60 мин; запустите позже."
    deadline = now_ns() + int(duration_s * NS_PER_S)
    requests = 0
    while now_ns() < deadline:
        for fixture_id in targets:
            await _json(ctx, "/odds", {"fixtureId": fixture_id, "bookmakers": bookmaker})
            requests += 1
        await asyncio.sleep(interval_s)
    await ctx.sink.flush()
    stats = odds_latency(iter_recorded_odds(connect(), ctx.raw_root), bookmaker)
    return f"Матчей: {len(targets)}, запросов: {requests}.\n\n" + render_latency(stats, bookmaker)


def _tournaments_from_store(ctx: EvalContext) -> dict[SportName, dict[int, tuple[str, str]]]:
    con = connect()
    return {s: load_tournaments(con, ctx.raw_root, sid) for s, sid in ctx.sport_ids.items()}


async def run_eval(settings: Settings, cfg: AppConfig, step: str, **kwargs: Any) -> str:
    odds_cfg = cfg.recorder.oddspapi
    sink = ParquetSink(settings.data_dir / "raw", flush_interval_s=3600)
    if step == "summary":
        return summary_tables(cfg, sink.root)
    budget = RequestBudget(
        settings.data_dir / "state" / "oddspapi_budget.json",
        odds_cfg.monthly_request_budget,
        odds_cfg.daily_request_budget,
    )
    try:
        async with make_client(cfg.base.http) as http:
            ctx = EvalContext(
                settings=settings,
                cfg=cfg,
                sink=sink,
                client=OddsPapiClient(http, odds_cfg, settings.oddspapi_api_key, sink, budget),
                gamma=GammaClient(http, cfg.base.polymarket.gamma_url, sink),
            )
            if step == "meta":
                text = await step_meta(ctx)
            elif step == "fixtures":
                text = await step_fixtures(ctx)
            elif step == "coverage":
                text = await step_coverage(
                    ctx, kwargs.get("bookmaker", "pinnacle"), kwargs.get("max_calls", 10)
                )
            elif step == "sample":
                text = await step_sample(ctx, kwargs.get("per_group", 8))
            elif step == "burst":
                text = await step_burst(
                    ctx,
                    kwargs.get("n_fixtures", 2),
                    kwargs.get("duration_s", 180.0),
                    kwargs.get("interval_s", 5.0),
                    kwargs.get("bookmaker", "pinnacle"),
                )
            else:
                raise ValueError(f"unknown step {step!r}")
    finally:
        await sink.close()
    month_left, day_left = budget.remaining()
    return text + f"\n\nОстаток бюджета запросов: месяц {month_left}, сегодня {day_left}."

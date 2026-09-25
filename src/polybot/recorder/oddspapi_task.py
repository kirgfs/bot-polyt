"""OddsPapi polling inside the recorder (mode `paid_rest`): only tournaments that matter.

Fixtures are refreshed every few hours; Polymarket events from discovery are matched
to fixtures; odds are polled per tournament with `/odds-by-tournaments` (one bookmaker
per call, many tournaments per call), faster close to the start.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import timedelta

from polybot.core.config import OddsPapiConfig, SportName
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns, ns_to_datetime
from polybot.feeds.odds.oddspapi import OddsPapiClient
from polybot.feeds.odds.oddspapi_models import OpFixture, parse_fixtures
from polybot.matching.event_matcher import MatcherConfig, match_event
from polybot.recorder.discovery import Discovery

log = get_logger(__name__)

TOURNAMENTS_PER_CALL = 10


def iso_utc(ts_ns: int) -> str:
    return ns_to_datetime(ts_ns).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunks(items: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class OddsPapiPaidRest:
    def __init__(
        self,
        client: OddsPapiClient,
        cfg: OddsPapiConfig,
        discovery: Discovery,
        sports: Iterable[SportName],
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._discovery = discovery
        self._sports = tuple(sports)
        self._matcher = MatcherConfig()
        self.fixtures: dict[SportName, list[OpFixture]] = {}
        self._fixtures_mono: dict[SportName, int] = {}
        self._odds_mono: dict[tuple[str, int], int] = {}
        self.matched_tournaments: dict[int, int] = {}  # tournament id -> nearest start ns

    async def run(self) -> None:
        while True:
            await self.refresh_fixtures()
            self.update_tournaments()
            await self.poll_odds()
            await asyncio.sleep(10)

    async def refresh_fixtures(self) -> None:
        paid = self._cfg.paid_rest
        for sport in self._sports:
            last = self._fixtures_mono.get(sport)
            if last is not None and mono_ns() - last < paid.fixtures_interval_s * NS_PER_S:
                continue
            now = now_ns()
            until = now + int(timedelta(days=paid.fixtures_window_days).total_seconds() * NS_PER_S)
            response = await self._client.get(
                "/fixtures",
                {"sportId": self._cfg.sport_ids[sport], "from": iso_utc(now), "to": iso_utc(until)},
            )
            if response is None or not response.ok:
                continue
            self.fixtures[sport] = parse_fixtures(response.json())
            self._fixtures_mono[sport] = mono_ns()
            log.info("oddspapi_fixtures", sport=sport, n=len(self.fixtures[sport]))

    def update_tournaments(self) -> None:
        result = self._discovery.last_result
        if result is None:
            return
        tournaments: dict[int, int] = {}
        for event in result.events.values():
            fixtures = self.fixtures.get(event.sport, [])
            if not fixtures:
                continue
            match = match_event(
                event, fixtures, sport_id=self._cfg.sport_ids.get(event.sport), cfg=self._matcher
            )
            if match.fixture_id is None:
                continue
            fixture = next(f for f in fixtures if f.fixture_id == match.fixture_id)
            if fixture.tournament_id is None or fixture.start_ns is None:
                continue
            current = tournaments.get(fixture.tournament_id)
            if current is None or fixture.start_ns < current:
                tournaments[fixture.tournament_id] = fixture.start_ns
        self.matched_tournaments = tournaments

    async def poll_odds(self) -> None:
        paid = self._cfg.paid_rest
        now = now_ns()
        near_ns = int(paid.near_window_h * 3600 * NS_PER_S)
        for bookmaker in self._cfg.bookmakers:
            due: list[int] = []
            for tournament_id, start in self.matched_tournaments.items():
                interval = (
                    paid.odds_interval_near_s
                    if start - now <= near_ns
                    else paid.odds_interval_far_s
                )
                last = self._odds_mono.get((bookmaker, tournament_id))
                if last is None or mono_ns() - last >= interval * NS_PER_S:
                    due.append(tournament_id)
            for batch in chunks(sorted(due), TOURNAMENTS_PER_CALL):
                response = await self._client.get(
                    "/odds-by-tournaments",
                    {"bookmaker": bookmaker, "tournamentIds": ",".join(map(str, batch))},
                )
                if response is None:
                    return  # budget exhausted: nothing else will succeed today
                if response.ok:
                    stamp = mono_ns()
                    for tournament_id in batch:
                        self._odds_mono[(bookmaker, tournament_id)] = stamp

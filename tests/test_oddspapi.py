from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from polybot.analytics.oddspapi_quality import (
    coverage,
    odds_latency,
    render_coverage,
    winner_prices,
)
from polybot.core.config import OddsPapiConfig
from polybot.core.timeutil import NS_PER_S, parse_ts_ns
from polybot.data.records import Kind, Source
from polybot.feeds.odds.oddspapi import OddsPapiClient, OddsPapiConfigError, RequestBudget
from polybot.feeds.odds.oddspapi_models import digits_id, iter_prices, parse_fixtures
from polybot.venues.polymarket.markets import ParseIssues, parse_event
from tests.conftest import ListWriter, load_json

SECRET = "sk-TESTKEY-123"
DAY = parse_ts_ns("2026-09-25T10:00:00Z") or 0


def fixture_payload() -> list[dict[str, Any]]:
    # Shape per OddsPapi docs excerpts ([2nd], docs/data_sources.md §5).
    return [
        {
            "fixtureId": "id1000001761300517",
            "participant1Id": 1,
            "participant1Name": "Lehecka J.",
            "participant2Id": 2,
            "participant2Name": "Fils A.",
            "sportId": 12,
            "tournamentId": 7,
            "tournamentName": "Cincinnati",
            "categoryName": "ATP",
            "statusId": 0,
            "startTime": "2026-08-18T01:15:00Z",
            "hasOdds": True,
            "externalProviders": {"betradarId": 61098461, "sofascoreId": 1},
        },
        {"fixtureId": "id2", "participant1Name": "A", "participant2Name": "B", "sportId": 12},
        {"no": "id"},
    ]


def odds_payload(changed: str, bookmaker_changed: str, price: float = 1.9) -> dict[str, Any]:
    return {
        "fixtureId": "id1000001761300517",
        "bookmakerOdds": {
            "pinnacle": {
                "markets": {
                    "171": {
                        "outcomes": {
                            "171": {
                                "players": {
                                    "0": {
                                        "price": price,
                                        "limit": 1000,
                                        "active": True,
                                        "changedAt": changed,
                                        "bookmakerChangedAt": bookmaker_changed,
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "softbook": {
                "markets": {"171": {"outcomes": {"171": {"players": {"0": {"price": 1.8}}}}}}
            },
        },
    }


class TestBudget:
    def test_persists_and_rolls_over(self, tmp_path: Path) -> None:
        path = tmp_path / "budget.json"
        budget = RequestBudget(path, monthly=3, daily=2)
        assert budget.try_consume(DAY) and budget.try_consume(DAY)
        assert not budget.try_consume(DAY)  # daily cap
        again = RequestBudget(path, monthly=3, daily=2)  # restart keeps counters
        assert not again.try_consume(DAY)
        next_day = DAY + 86400 * NS_PER_S
        assert again.try_consume(next_day)
        assert not again.try_consume(next_day)  # monthly cap (3)
        next_month = parse_ts_ns("2026-10-01T00:00:00Z") or 0
        assert again.remaining(next_month) == (3, 2)


def make_client(
    tmp_path: Path, writer: ListWriter, handler: Any, *, monthly: int = 10
) -> OddsPapiClient:
    cfg = OddsPapiConfig(min_interval_s=0.0)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    budget = RequestBudget(tmp_path / "b.json", monthly=monthly, daily=monthly)
    return OddsPapiClient(http, cfg, SecretStr(SECRET), writer, budget)  # type: ignore[arg-type]


class TestClient:
    async def test_key_is_sent_but_never_recorded(self, tmp_path: Path) -> None:
        writer = ListWriter()
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, json=fixture_payload(), headers={"X-RateLimit-Remaining": "9"}
            )

        client = make_client(tmp_path, writer, handler)
        response = await client.get("/fixtures", {"sportId": 12}, key="tennis")
        assert response is not None and response.ok
        assert seen[0].url.params["apiKey"] == SECRET
        rest = writer.of(Source.ODDSPAPI_REST, Kind.REST)
        assert rest[0].endpoint == "/fixtures?sportId=12"
        assert rest[0].event_type == "fixtures"
        assert all(SECRET not in (r.endpoint or "") + r.payload for r in writer.records)
        quota = [r for r in writer.records if r.event_type == "quota_headers"]
        assert json.loads(quota[0].payload) == {"x-ratelimit-remaining": "9"}

    async def test_budget_exhaustion_stops_requests(self, tmp_path: Path) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=[])

        client = make_client(tmp_path, ListWriter(), handler, monthly=1)
        assert await client.get("/sports") is not None
        assert await client.get("/sports") is None
        assert len(calls) == 1

    def test_missing_key_is_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(OddsPapiConfigError):
            OddsPapiClient(
                httpx.AsyncClient(),
                OddsPapiConfig(),
                None,
                ListWriter(),  # type: ignore[arg-type]
                RequestBudget(tmp_path / "b.json", 1, 1),
            )


class TestModels:
    def test_fixtures(self) -> None:
        fixtures = parse_fixtures(fixture_payload())
        assert [f.fixture_id for f in fixtures] == ["id1000001761300517", "id2"]
        first = fixtures[0]
        assert first.betradar_id == "61098461"
        assert first.start_ns == parse_ts_ns("2026-08-18T01:15:00Z")
        assert parse_fixtures({"data": fixture_payload()})[0].fixture_id == "id1000001761300517"

    def test_prices(self) -> None:
        prices = list(iter_prices(odds_payload("2026-08-18T00:00:02Z", "2026-08-18T00:00:01Z")))
        pinnacle = next(p for p in prices if p.bookmaker == "pinnacle")
        assert (pinnacle.market_id, pinnacle.outcome_id, pinnacle.price) == ("171", "171", 1.9)
        assert pinnacle.changed_at_ns is not None and pinnacle.bookmaker_changed_at_ns is not None

    def test_digits_id(self) -> None:
        assert digits_id("sr:match:61098461") == digits_id(61098461) == "61098461"
        assert digits_id(None) is None and digits_id("abc") is None


class TestQuality:
    def test_latency_uses_only_fresh_changes(self) -> None:
        t0 = parse_ts_ns("2026-08-18T00:00:00Z") or 0
        observations = [
            (t0 + 5 * NS_PER_S, odds_payload("2026-08-18T00:00:02Z", "2026-08-18T00:00:01Z")),
            (t0 + 6 * NS_PER_S, odds_payload("2026-08-18T00:00:02Z", "2026-08-18T00:00:01Z")),
            (
                t0 + 12 * NS_PER_S,
                odds_payload("2026-08-18T00:00:11Z", "2026-08-18T00:00:10.5Z", 1.95),
            ),
        ]
        stats = odds_latency(observations)
        assert stats.fresh_changes == 1  # first sighting skipped, repeat ignored
        assert stats.ingest_ms.p50 == pytest.approx(500)
        assert stats.e2e_ms.p50 == pytest.approx(1500)
        assert stats.update_interval_s.n == 0

    def test_coverage_and_validation(self) -> None:
        raw = load_json("gamma_events_tennis.json")["events"]
        event = parse_event(raw[0], "tennis", ParseIssues())
        fixtures = parse_fixtures(fixture_payload())
        by_id = {f.fixture_id: f for f in fixtures}
        prices = winner_prices(
            [(0, odds_payload("2026-08-18T00:00:02Z", "2026-08-18T00:00:01Z"))], by_id, {12: 171}
        )
        assert prices == {"id1000001761300517": {"pinnacle", "softbook"}}
        result = coverage(
            [event],
            fixtures_by_sport={"tennis": fixtures},
            tournaments_by_sport={"tennis": {7: ("Cincinnati", "ATP")}},
            sport_ids={"tennis": 12},
            prices=prices,
        )
        cell = result.cells[("tennis", "atp_wta")]
        assert (
            cell.pm_events == 1
            and cell.by_method["betradar_id"] == 1
            and cell.with_book["pinnacle"] == 1
        )
        assert result.validation.agree == 1 and result.validation.wrong == 0
        assert "100.0%" in render_coverage(result)

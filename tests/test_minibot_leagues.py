"""Leagues by Gamma /sports codes, and discovery through series listings.

The /sports rows are copied from the VPS probe of 2026-09-26 [CAP] (docs/api_notes.md §11).
"""

from __future__ import annotations

from typing import Any

import httpx

from polybot.core.timeutil import now_ns
from polybot.minibot.leagues import League, league_of_slug, resolve_leagues, sport_codes
from polybot.recorder.discovery import Discovery
from polybot.venues.polymarket.gamma import GammaClient
from tests.conftest import ListWriter
from tests.minibot_helpers import H, mini_config, raw_event

SPORTS: list[dict[str, Any]] = [
    {"sport": "lal", "tags": "1,780,100639,100350", "series": "10193"},
    {"sport": "ere", "tags": "1,100639,100350,101735", "series": "10286"},
    {"sport": "es2", "tags": "1,100639,100350,102866", "series": "10672"},
    {"sport": "fl1", "tags": "1,100639,102070,100350", "series": "10195"},
    {"sport": "sea", "tags": "1,100639,101962,100350,100618", "series": "10203"},
    {"sport": "bkseriea", "tags": "1,100639,28,103095", "series": "10877"},
    {"sport": "bra2", "tags": "1,100639,100350,105921", "series": "10973"},
]
SOCCER = 100350
GENERIC = [1, 100639, SOCCER]  # sports, games, soccer


def test_codes_resolve_to_series_and_own_tags() -> None:
    found, missing = resolve_leagues(SPORTS, ["sea", "lal", "fl1", "ere", "epl"], GENERIC)
    assert found == {
        "sea": League("sea", (101962, 100618), (10203,)),
        "lal": League("lal", (780,), (10193,)),
        "fl1": League("fl1", (102070,), (10195,)),
        "ere": League("ere", (101735,), (10286,)),
    }
    assert missing == ["epl"]
    # sports (1), games (100639) and soccer (100350) are shared: never listed as a league tag.
    assert not {1, 100639, SOCCER} & {t for lg in found.values() for t in lg.tag_ids}


def test_bad_or_short_sports_response_never_lists_shared_tags() -> None:
    assert resolve_leagues({"error": "oops"}, ["sea"], GENERIC) == ({}, ["sea"])
    # One entry: the shared tags are not "rare" here, the explicit exclusion still holds.
    short = [{"sport": "sea", "tags": "1,100639,100350", "series": ""}]
    assert resolve_leagues(short, ["sea"], GENERIC) == ({}, ["sea"])


def test_soccer_codes_for_the_dry_run() -> None:
    codes = [league.code for league in sport_codes(SPORTS, SOCCER, GENERIC)]
    assert codes == ["bra2", "ere", "es2", "fl1", "lal", "sea"]  # no basketball Serie A


def test_league_from_match_slug() -> None:
    assert league_of_slug("lal-mala-esp-2026-10-09", ["lal", "fl1"]) == "lal"
    assert league_of_slug("es2-gra-and-2026-09-26", ["lal", "fl1"]) == ""
    assert league_of_slug("", ["lal"]) == ""


async def test_discovery_pages_series_and_tags_once_per_event() -> None:
    event = raw_event("1000", now_ns() + 5 * H, league="lal")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"events": [event], "next_cursor": None})

    writer = ListWriter()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    discovery = Discovery(
        GammaClient(client, "https://gamma.test", writer),
        mini_config(leagues=["lal"]).recorder_view(),
        writer,
    )
    discovery.tag_ids = {"soccer": [780]}
    discovery.series_ids = {"soccer": [10193]}
    result = await discovery.poll()
    await client.aclose()
    listings = [(r.url.params.get("tag_id"), r.url.params.get("series_id")) for r in requests]
    assert listings == [("780", None), (None, "10193")]
    assert list(result.events) == ["1000"] and len(result.selected) == 3


async def test_dry_run_reports_leagues_matches_and_codes(monkeypatch: Any) -> None:
    import polybot.minibot.app as app  # noqa: PLC0415 - patched below

    start = now_ns() + 13 * 24 * H  # next round, two weeks out
    match = raw_event("1000", start, league="lal", home="Malaga", away="Espanyol")
    future = {
        "id": "9",
        "slug": "laliga-2027-champion",
        "title": "LALIGA: 2027 Champion",
        "markets": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/geoblock":
            return httpx.Response(200, json={"blocked": False, "country": "AM"})
        if path == "/sports":
            return httpx.Response(200, json=SPORTS)
        if path.startswith("/tags/slug/"):
            ids = {"sports": 1, "games": 100639, "soccer": SOCCER}
            return httpx.Response(200, json={"id": ids[path.rsplit("/", 1)[1]]})
        if path == "/events/keyset":
            return httpx.Response(200, json={"events": [match, future]})
        return httpx.Response(404)

    monkeypatch.setattr(
        app, "make_client", lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    base = app.BaseConfig.model_validate({"geoblock": {"allowed_countries": ["AM"]}})
    text, code = await app.dry_run(base, mini_config(leagues=["lal", "epl"]))
    assert code == app.EXIT_STARTUP and "Коды ['epl'] не найдены" in text
    row = next(line for line in text.splitlines() if line.startswith("| lal |"))
    assert "| Ла Лига | 10193 | 780 | 1 / 1 |" in row
    assert "| sea | 10203 | 101962, 100618 |" in text  # the soccer codes table
    assert "moneyline" in text and "Malaga vs. Espanyol" in text

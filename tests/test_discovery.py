from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest

from polybot.core.config import AppConfig
from polybot.core.timeutil import NS_PER_S, now_ns, parse_ts_ns
from polybot.data.records import Kind, Source
from polybot.recorder.discovery import (
    Discovery,
    cap_markets,
    slim_event,
    structural_fingerprint,
)
from polybot.venues.polymarket.gamma import GammaClient
from polybot.venues.polymarket.markets import PmEvent, PmMarket
from tests.conftest import ListWriter, gamma_page


def gamma_client(
    pages: dict[str, Any], writer: ListWriter
) -> tuple[GammaClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.startswith("/tags/slug/"):
            slug = request.url.path.rsplit("/", 1)[-1]
            ids = {"tennis": "864", "soccer": "100350", "basketball": "28", "games": "100639"}
            return httpx.Response(200, json={"id": ids[slug]})
        if request.url.path == "/events/keyset":
            tag = request.url.params["tag_id"]
            return httpx.Response(200, json=pages.get(tag, {"events": [], "next_cursor": None}))
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gamma.test"
    )
    return GammaClient(client, "https://gamma.test", writer), seen


async def test_poll_selects_books_and_dedupes(app_config: AppConfig) -> None:
    writer = ListWriter()
    start = now_ns() + 2 * 3600 * NS_PER_S
    gamma, requests = gamma_client({"864": gamma_page(start)}, writer)
    discovery = Discovery(gamma, app_config.recorder, writer)
    await discovery.resolve_tags()
    assert discovery.tag_ids == {"tennis": [864], "soccer": [100350], "basketball": [28]}

    first = await discovery.poll()
    # Singles moneyline only: two events, two tokens each; doubles and futures excluded.
    assert first.token_ids == {
        "1111111111111111111111111111111111111111111111111111111111111111111111111101",
        "1111111111111111111111111111111111111111111111111111111111111111111111111102",
        "401",
        "402",
    }
    assert {e.slug for e in first.events.values()} >= {"atp-lehecka-fils-2026-08-17"}
    assert "2026-mens-us-open-winner-tennis" not in {e.slug for e in first.events.values()}
    written = [r for r in writer.of(Source.GAMMA_EVENTS, Kind.REST)]
    assert len(written) == first.written == 3  # match events incl. doubles are tracked
    assert all(r.key and r.event_type == "tennis" for r in written)
    assert all(
        req.url.params.get("closed") == "false"
        for req in requests
        if req.url.path == "/events/keyset"
    )

    second = await discovery.poll()
    assert second.written == 0  # nothing structural changed, full snapshot not due yet


async def test_structural_change_is_written_and_gone_events_marked(app_config: AppConfig) -> None:
    writer = ListWriter()
    start = now_ns() + 2 * 3600 * NS_PER_S
    page = gamma_page(start)
    gamma, _ = gamma_client({"864": page}, writer)
    discovery = Discovery(gamma, app_config.recorder, writer)
    await discovery.resolve_tags()
    await discovery.poll()

    page["events"][0]["markets"][0]["outcomePrices"] = '["0.2", "0.8"]'  # volatile: ignored
    assert (await discovery.poll()).written == 0
    page["events"][0]["markets"][0]["secondsDelay"] = 1  # structural: written
    assert (await discovery.poll()).written == 1

    page["events"].pop(1)
    await discovery.poll()
    gone = [r for r in writer.of(Source.GAMMA_EVENTS, Kind.CONTROL) if r.event_type == "event_gone"]
    assert [r.key for r in gone] == ["856321"]


def test_fingerprint_ignores_volatile_fields() -> None:
    a = {"id": "1", "markets": [{"volumeNum": 1, "gameStartTime": "x"}]}
    b = {"id": "1", "markets": [{"volumeNum": 2, "gameStartTime": "x"}]}
    c = {"id": "1", "markets": [{"volumeNum": 2, "gameStartTime": "y"}]}
    assert structural_fingerprint(a) == structural_fingerprint(b) != structural_fingerprint(c)


async def test_required_tags_and_truncation(app_config: AppConfig) -> None:
    writer = ListWriter()
    page = gamma_page(now_ns() + 2 * 3600 * NS_PER_S)
    page["next_cursor"] = "more"  # the server always says there is another page
    gamma, requests = gamma_client({"864": page}, writer)
    cfg = app_config.recorder.model_copy(
        update={
            "sports": {
                "tennis": app_config.recorder.sports["tennis"].model_copy(
                    update={"require_tag_slugs": ("games",)}
                )
            },
            "discovery": app_config.recorder.discovery.model_copy(update={"max_pages": 2}),
        }
    )
    discovery = Discovery(gamma, cfg, writer)
    await discovery.resolve_tags()
    result = await discovery.poll()
    listing = [r for r in requests if r.url.path == "/events/keyset"]
    assert len(listing) == 2
    assert listing[0].url.params.get_list("tag_id") == ["864", "100639"]
    assert listing[0].url.params["tag_match"] == "all"
    assert listing[1].url.params["after_cursor"] == "more"
    assert result.token_ids  # partial listing is still used
    summary = [
        r for r in writer.of(Source.GAMMA_EVENTS, Kind.CONTROL) if r.event_type == "poll_summary"
    ]
    assert json.loads(summary[-1].payload)["truncated"] == ["tennis:864"]


class DrainCountingWriter(ListWriter):
    def __init__(self) -> None:
        super().__init__()
        self.drains = 0

    async def drain(self) -> None:
        self.drains += 1


async def test_pages_are_processed_one_at_a_time(app_config: AppConfig) -> None:
    writer = DrainCountingWriter()
    events = gamma_page(now_ns() + 2 * 3600 * NS_PER_S)["events"]
    pages = {"": {"events": events[:2], "next_cursor": "p2"}, "p2": {"events": events[2:]}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/tags/slug/"):
            return httpx.Response(200, json={"id": "864"})
        return httpx.Response(200, json=pages[request.url.params.get("after_cursor", "")])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = app_config.recorder.model_copy(
        update={"sports": {"tennis": app_config.recorder.sports["tennis"]}}
    )
    discovery = Discovery(GammaClient(client, "https://gamma.test", writer), cfg, writer)
    await discovery.resolve_tags()
    result = await discovery.poll()
    assert writer.drains == 2  # the sink may flush after every page
    assert len(result.token_ids) == 4  # both pages were used
    # Only markets of the recorded types are kept between polls.
    kept_types = {m.sports_market_type for e in result.events.values() for m in e.markets}
    assert kept_types == {"moneyline"}


async def test_full_snapshot_at_utc_day_change(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = ListWriter()
    clock = {"now": parse_ts_ns("2026-09-20T23:50:00Z") or 0}
    monkeypatch.setattr("polybot.recorder.discovery.now_ns", lambda: clock["now"])
    gamma, _ = gamma_client({"864": gamma_page(clock["now"] + 2 * 3600 * NS_PER_S)}, writer)
    discovery = Discovery(gamma, app_config.recorder, writer)
    await discovery.resolve_tags()
    assert (await discovery.poll()).full_snapshot
    clock["now"] += 5 * 60 * NS_PER_S  # 23:55, same day
    assert not (await discovery.poll()).full_snapshot
    clock["now"] += 10 * 60 * NS_PER_S  # 00:05 next day: the new date partition gets everything
    third = await discovery.poll()
    assert third.full_snapshot and third.written == 3


def test_cap_keeps_markets_nearest_to_start() -> None:
    now = 1_000 * 3600 * NS_PER_S

    def market(cid: str, hours: float) -> tuple[PmEvent, PmMarket]:
        m = PmMarket(
            market_id=cid, condition_id=cid, question="", slug="", sports_market_type="moneyline",
            line=None, outcomes=("A", "B"), token_ids=(f"{cid}a", f"{cid}b"),
            game_start_ns=now + int(hours * 3600 * NS_PER_S), game_start_raw=None, game_id=None,
            active=True, closed=False, accepting_orders=True, enable_order_book=True,
            neg_risk=False, tick_size=None, min_order_size=None, seconds_delay=None,
            has_description=True,
        )  # fmt: skip
        e = PmEvent(
            event_id=cid, slug="", title="", sport="tennis", tag_slugs=(), game_id=None,
            sportsradar_match_id=None, home_name=None, away_name=None, live=None, ended=None,
            closed=None, is_doubles=False, markets=(m,),
        )  # fmt: skip
        return e, m

    selected = [market("far", 60), market("live", -1), market("soon", 2), market("old", -5)]
    kept, dropped = cap_markets(selected, 2, now)
    assert [m.condition_id for _, m in kept] == ["live", "soon"] and dropped == 2
    assert cap_markets(selected, 10, now) == (selected, 0)
    side = dataclasses.replace(selected[0][1], condition_id="side", sports_market_type="totals")
    slim = slim_event(dataclasses.replace(selected[0][0], markets=(side,)), frozenset({"x"}))
    assert [m.condition_id for m in slim.markets] == ["side"]  # start time stays known

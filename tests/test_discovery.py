from __future__ import annotations

import json
from typing import Any

import httpx

from polybot.core.config import AppConfig
from polybot.core.timeutil import NS_PER_S, now_ns
from polybot.data.records import Kind, Source
from polybot.recorder.discovery import Discovery, structural_fingerprint
from polybot.venues.polymarket.gamma import GammaClient
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

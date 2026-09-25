"""Gamma REST reader (docs/api_notes.md §11): tags, sports metadata, events via keyset pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from polybot.core.http import TimedResponse, timed_request
from polybot.data.records import Kind, Record, RecordWriter, Source


class GammaError(RuntimeError):
    pass


@dataclass
class EventsPage:
    events: list[dict[str, Any]] = field(default_factory=list)
    latency_ns: int = 0
    requests: int = 0
    truncated: bool = False


class GammaClient:
    def __init__(self, client: httpx.AsyncClient, base_url: str, sink: RecordWriter | None) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._sink = sink

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, record_as: Source | None = None
    ) -> TimedResponse:
        response = await timed_request(self._client, "GET", self._base + path, params=params)
        if record_as is not None and self._sink is not None:
            self._sink.write(
                Record(
                    ts_recv_ns=response.ts_recv_ns,
                    source=record_as,
                    kind=Kind.REST,
                    payload=response.text,
                    endpoint=path,
                    status=response.status,
                    latency_ns=response.latency_ns,
                )
            )
        if not response.ok:
            raise GammaError(f"GET {path} -> HTTP {response.status}")
        return response

    async def resolve_tag_id(self, slug: str) -> int:
        response = await self._get(
            f"/tags/slug/{quote(slug, safe='')}", record_as=Source.GAMMA_META
        )
        data = response.json()
        tag_id = data.get("id") if isinstance(data, dict) else None
        try:
            return int(str(tag_id))
        except ValueError as exc:
            raise GammaError(f"tag slug {slug!r}: no numeric id in response") from exc

    async def get_sports(self) -> Any:
        return (await self._get("/sports", record_as=Source.GAMMA_META)).json()

    async def get_market_types(self) -> Any:
        return (await self._get("/sports/market-types", record_as=Source.GAMMA_META)).json()

    async def list_events(
        self,
        *,
        tag_id: int,
        page_size: int,
        max_pages: int,
        require_tag_ids: tuple[int, ...] = (),
    ) -> EventsPage:
        """Open events for a tag via `/events/keyset` (docs/api_notes.md §11).

        With `require_tag_ids`, events must carry all tags (`tag_match=all`). When
        pagination does not finish within `max_pages`, the page is marked truncated
        instead of failing: stale subscriptions are worse than a flagged partial list.
        """
        tags = (tag_id, *require_tag_ids)
        params: dict[str, Any] = {"tag_id": list(tags), "closed": "false", "limit": page_size}
        if require_tag_ids:
            params["tag_match"] = "all"
        page = EventsPage()
        cursor: str | None = None
        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["after_cursor"] = cursor
            response = await self._get("/events/keyset", page_params)
            page.latency_ns += response.latency_ns
            page.requests += 1
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                raise GammaError("keyset response without an 'events' array")
            page.events.extend(e for e in data["events"] if isinstance(e, dict))
            next_cursor = data.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return page
            cursor = next_cursor
        page.truncated = True
        return page

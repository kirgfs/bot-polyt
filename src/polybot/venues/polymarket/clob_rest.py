"""Public (unauthenticated) CLOB REST reads used by the recorder (docs/api_notes.md §2)."""

from __future__ import annotations

from typing import Any

import httpx

from polybot.core.http import TimedResponse, timed_request


class ClobPublic:
    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")

    async def server_time(self) -> TimedResponse:
        return await timed_request(self._client, "GET", self._base + "/time")

    async def book(self, token_id: str) -> TimedResponse:
        return await timed_request(
            self._client, "GET", self._base + "/book", params={"token_id": token_id}
        )

    async def books(self, token_ids: list[str]) -> TimedResponse:
        body: list[dict[str, Any]] = [{"token_id": t} for t in token_ids]
        return await timed_request(self._client, "POST", self._base + "/books", json_body=body)

    async def clob_market(self, condition_id: str) -> TimedResponse:
        return await timed_request(self._client, "GET", f"{self._base}/clob-markets/{condition_id}")

    async def rewards_current(self, cursor: str | None) -> TimedResponse:
        params = {"next_cursor": cursor} if cursor else None
        return await timed_request(
            self._client, "GET", self._base + "/rewards/markets/current", params=params
        )


def cf_colo(headers: dict[str, str] | Any) -> str | None:
    """Cloudflare data center from the `cf-ray` header suffix (docs/api_notes.md §15)."""
    ray = headers.get("cf-ray") if hasattr(headers, "get") else None
    if not isinstance(ray, str) or "-" not in ray:
        return None
    return ray.rsplit("-", 1)[1] or None

"""Shared async HTTP helpers with timing, used by all REST readers."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from polybot.core.config import HttpConfig
from polybot.core.timeutil import now_ns


@dataclass(frozen=True, slots=True)
class TimedResponse:
    status: int
    text: str
    headers: Mapping[str, str]
    latency_ns: int  # request start → full body received (perf counter)
    ts_recv_ns: int  # wall clock when the body was received

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def make_client(cfg: HttpConfig, *, base_url: str = "") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(cfg.timeout_s),
        headers={"User-Agent": cfg.user_agent, "Accept": "application/json"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=False,
    )


async def timed_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
) -> TimedResponse:
    """Send a request and time it. Network errors propagate as httpx.HTTPError."""
    started = time.perf_counter_ns()
    response = await client.request(method, url, params=params, json=json_body)
    text = response.text
    latency = time.perf_counter_ns() - started
    return TimedResponse(
        status=response.status_code,
        text=text,
        headers=dict(response.headers),
        latency_ns=latency,
        ts_recv_ns=now_ns(),
    )

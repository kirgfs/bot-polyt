"""Geoblock gate (docs/api_notes.md §14). Fail-closed: anything but a clean "allowed" is a no.

Start is allowed only when the response is parsed, `blocked` is exactly false and the
country is in the operator's allowlist. We never try to get around a block.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

import httpx

from polybot.core.config import GeoblockConfig
from polybot.core.http import timed_request
from polybot.core.logging import get_logger
from polybot.data.records import Kind, Record, RecordWriter, Source

log = get_logger(__name__)


class GeoVerdict(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"  # blocked == true
    COUNTRY_NOT_ALLOWED = "country_not_allowed"  # blocked == false, but not our jurisdiction
    ERROR = "error"  # network error, bad status or unparseable body


@dataclass(frozen=True, slots=True)
class GeoStatus:
    verdict: GeoVerdict
    country: str | None
    region: str | None
    detail: str

    @property
    def allowed(self) -> bool:
        return self.verdict is GeoVerdict.ALLOWED


def _mask_ip(ip: object) -> str | None:
    if not isinstance(ip, str) or not ip:
        return None
    if "." in ip:
        return ".".join([*ip.split(".")[:3], "x"])
    return ip.split(":")[0] + ":…"


def evaluate_geoblock(status: int, body: str, allowed_countries: tuple[str, ...]) -> GeoStatus:
    """Pure decision function over the raw HTTP response."""
    if status != 200:
        return GeoStatus(GeoVerdict.ERROR, None, None, f"http status {status}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return GeoStatus(GeoVerdict.ERROR, None, None, "body is not JSON")
    if not isinstance(data, dict):
        return GeoStatus(GeoVerdict.ERROR, None, None, "body is not an object")
    blocked = data.get("blocked")
    country = data.get("country")
    region = data.get("region")
    country_s = country.strip().upper() if isinstance(country, str) and country.strip() else None
    region_s = region if isinstance(region, str) else None
    if blocked is True:
        return GeoStatus(GeoVerdict.BLOCKED, country_s, region_s, "blocked=true")
    if blocked is not False:
        # Missing or non-boolean flag: unknown format, treat as not allowed.
        return GeoStatus(GeoVerdict.ERROR, country_s, region_s, f"unexpected blocked={blocked!r}")
    if country_s is None or country_s not in allowed_countries:
        return GeoStatus(
            GeoVerdict.COUNTRY_NOT_ALLOWED,
            country_s,
            region_s,
            f"country {country_s!r} not in {list(allowed_countries)}",
        )
    return GeoStatus(GeoVerdict.ALLOWED, country_s, region_s, "ok")


async def check_geoblock(
    client: httpx.AsyncClient, cfg: GeoblockConfig, sink: RecordWriter | None = None
) -> GeoStatus:
    try:
        response = await asyncio.wait_for(timed_request(client, "GET", cfg.url), cfg.timeout_s)
    except (httpx.HTTPError, TimeoutError) as exc:
        status = GeoStatus(GeoVerdict.ERROR, None, None, f"request failed: {type(exc).__name__}")
        log.warning("geoblock_check_failed", detail=status.detail)
        return status
    status = evaluate_geoblock(response.status, response.text, cfg.allowed_countries)
    if sink is not None:
        # Store the verdict, not the raw body: the body contains our server IP.
        payload = {
            "verdict": status.verdict.value,
            "country": status.country,
            "region": status.region,
            "detail": status.detail,
            "ip_masked": _mask_ip(_safe_ip(response.text)),
        }
        sink.write(
            Record(
                ts_recv_ns=response.ts_recv_ns,
                source=Source.GEOBLOCK,
                kind=Kind.REST,
                payload=json.dumps(payload),
                endpoint=cfg.url,
                status=response.status,
                latency_ns=response.latency_ns,
                event_type=status.verdict.value,
            )
        )
    log.info(
        "geoblock_status",
        verdict=status.verdict.value,
        country=status.country,
        region=status.region,
        detail=status.detail,
    )
    return status


def _safe_ip(body: str) -> object:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return data.get("ip") if isinstance(data, dict) else None


class GeoGuard:
    """Periodic re-check. Calls `on_violation` once when the process must stop.

    A response saying blocked or a foreign country stops immediately. Network errors are
    tolerated up to `max_consecutive_errors` in a row: the recorder carries no trading
    risk, and a single failed request should not create a data gap. The trading bot
    (M4) cancels all orders on the first failure instead.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        cfg: GeoblockConfig,
        on_violation: Callable[[GeoStatus], Awaitable[None]],
        sink: RecordWriter | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._on_violation = on_violation
        self._sink = sink
        self._errors = 0
        self.last_status: GeoStatus | None = None

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.interval_s)
            status = await check_geoblock(self._client, self._cfg, self._sink)
            self.last_status = status
            if status.allowed:
                self._errors = 0
                continue
            if status.verdict is GeoVerdict.ERROR:
                self._errors += 1
                if self._errors < self._cfg.max_consecutive_errors:
                    continue
            log.critical("geoblock_violation_stopping", verdict=status.verdict.value)
            await self._on_violation(status)
            return

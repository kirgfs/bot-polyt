from __future__ import annotations

import json

import httpx
import pytest

from polybot.core.config import GeoblockConfig
from polybot.data.records import Source
from polybot.venues.polymarket.geoblock import (
    GeoGuard,
    GeoStatus,
    GeoVerdict,
    check_geoblock,
    evaluate_geoblock,
)
from tests.conftest import ListWriter

ALLOWED = ("AM",)


def body(**fields: object) -> str:
    return json.dumps(fields)


@pytest.mark.parametrize(
    ("status", "text", "verdict"),
    [
        (200, body(blocked=False, ip="1.2.3.4", country="AM", region="ER"), GeoVerdict.ALLOWED),
        (200, body(blocked=False, ip="1.2.3.4", country="am", region="ER"), GeoVerdict.ALLOWED),
        (200, body(blocked=True, ip="1.2.3.4", country="US", region="NY"), GeoVerdict.BLOCKED),
        # Not blocked, but not the operator's jurisdiction (e.g. mis-geolocated VPS IP).
        (
            200,
            body(blocked=False, ip="1.2.3.4", country="DE", region=""),
            GeoVerdict.COUNTRY_NOT_ALLOWED,
        ),
        (200, body(blocked=False, ip="1.2.3.4"), GeoVerdict.COUNTRY_NOT_ALLOWED),
        # Unknown format or transport problems: fail closed.
        (200, body(ip="1.2.3.4", country="AM"), GeoVerdict.ERROR),
        (200, body(blocked="false", country="AM"), GeoVerdict.ERROR),
        (200, "<html>blocked</html>", GeoVerdict.ERROR),
        (200, "[]", GeoVerdict.ERROR),
        (403, body(blocked=False, country="AM"), GeoVerdict.ERROR),
    ],
)
def test_evaluate(status: int, text: str, verdict: GeoVerdict) -> None:
    result = evaluate_geoblock(status, text, ALLOWED)
    assert result.verdict is verdict
    assert result.allowed is (verdict is GeoVerdict.ALLOWED)


def client_returning(responses: list[httpx.Response | Exception]) -> httpx.AsyncClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cfg(**overrides: object) -> GeoblockConfig:
    base: dict[str, object] = {"allowed_countries": ["AM"], "interval_s": 0.001, "timeout_s": 1.0}
    base.update(overrides)
    return GeoblockConfig.model_validate(base)


async def test_check_records_verdict_without_full_ip(writer: ListWriter) -> None:
    ok = httpx.Response(
        200, json={"blocked": False, "ip": "203.0.113.42", "country": "AM", "region": "ER"}
    )
    async with client_returning([ok]) as client:
        status = await check_geoblock(client, cfg(), writer)
    assert status.allowed
    (record,) = writer.of(Source.GEOBLOCK)
    assert "203.0.113.42" not in record.payload
    assert json.loads(record.payload)["ip_masked"] == "203.0.113.x"


async def test_network_error_is_not_allowed() -> None:
    async with client_returning([httpx.ConnectError("boom")]) as client:
        status = await check_geoblock(client, cfg())
    assert status.verdict is GeoVerdict.ERROR


async def test_guard_stops_immediately_when_blocked() -> None:
    stopped: list[GeoStatus] = []

    async def on_violation(status: GeoStatus) -> None:
        stopped.append(status)

    blocked = httpx.Response(200, json={"blocked": True, "country": "AM"})
    async with client_returning([blocked]) as client:
        await GeoGuard(client, cfg(), on_violation).run()
    assert [s.verdict for s in stopped] == [GeoVerdict.BLOCKED]


async def test_guard_tolerates_transient_errors_then_stops() -> None:
    stopped: list[GeoStatus] = []

    async def on_violation(status: GeoStatus) -> None:
        stopped.append(status)

    ok = httpx.Response(200, json={"blocked": False, "country": "AM"})
    responses: list[httpx.Response | Exception] = [
        httpx.ConnectError("1"),
        ok,  # resets the error counter
        httpx.ConnectError("2"),
        httpx.ConnectError("3"),
        httpx.ConnectError("4"),
    ]
    async with client_returning(responses) as client:
        await GeoGuard(client, cfg(max_consecutive_errors=3), on_violation).run()
    assert len(stopped) == 1
    assert stopped[0].verdict is GeoVerdict.ERROR

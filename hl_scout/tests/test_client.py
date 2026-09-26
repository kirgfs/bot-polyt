"""Info API client against a fake transport: weights, retries, pagination limits from docs/api_notes.md."""

from __future__ import annotations

import json

import httpx
import pytest

from hl_scout.config import ApiCfg
from hl_scout.hl.client import HyperliquidError, InfoClient
from hl_scout.hl.ratelimit import WeightLimiter


async def no_sleep(_: float) -> None:
    return None


def client_with(handler, **cfg) -> InfoClient:
    api = ApiCfg(**{"retries": 3, "backoff_base_s": 0.0, **cfg})
    return InfoClient(api, transport=httpx.MockTransport(handler), sleep=no_sleep)


async def test_weights_and_item_surcharge():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "clearinghouseState":
            return httpx.Response(200, json={"assetPositions": []})
        return httpx.Response(200, json=[{"time": i, "fundingRate": "0"} for i in range(100)])

    c = client_with(handler)
    await c.clearinghouse_state("0x" + "1" * 40)
    assert c.limiter.spent == 2
    await c.funding_history("BTC", 0, 10)
    assert c.limiter.spent == 2 + 20 + 100 // 20
    await c.aclose()


async def test_retries_on_429_and_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        if calls["n"] == 2:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"ok": True})

    c = client_with(handler)
    assert await c.info({"type": "meta"}) == {"ok": True}
    assert calls["n"] == 3
    await c.aclose()


async def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, text="bad request")

    c = client_with(handler)
    with pytest.raises(HyperliquidError):
        await c.info({"type": "meta"})
    assert calls["n"] == 1
    await c.aclose()


async def test_fills_pagination_advances_by_time_and_dedupes():
    fills = [{"time": 1000 + i, "tid": i, "oid": i, "px": "1", "sz": "1", "coin": "BTC"} for i in range(4500)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["type"] == "userFillsByTime" and body["aggregateByTime"] is True
        page = [f for f in fills if body["startTime"] <= f["time"] <= body["endTime"]][:2000]
        return httpx.Response(200, json=page)

    c = client_with(handler)
    res = await c.user_fills_by_time("0x" + "1" * 40, 0, 10**9)
    assert len(res.fills) == 4500
    assert res.pages == 3
    assert not res.truncated
    assert [f["time"] for f in res.fills] == sorted(f["time"] for f in res.fills)
    await c.aclose()


async def test_fills_near_10000_limit_are_flagged_truncated():
    fills = [{"time": 1000 + i, "tid": i, "oid": i, "px": "1", "sz": "1", "coin": "BTC"} for i in range(10_000)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=[f for f in fills if f["time"] >= body["startTime"]][:2000])

    c = client_with(handler)
    res = await c.user_fills_by_time("0x" + "1" * 40, 0, 10**9)
    assert res.truncated
    await c.aclose()


async def test_range_pagination_uses_500_limit():
    rows = [{"time": i, "hash": f"h{i}", "delta": {"type": "deposit", "usdc": "1"}} for i in range(1200)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=[r for r in rows if body["startTime"] <= r["time"] <= body["endTime"]][:500])

    c = client_with(handler)
    out = await c.ledger_updates("0x" + "1" * 40, 0, 10**6)
    assert len(out) == 1200
    await c.aclose()


async def test_candles_never_ask_beyond_the_5000_window():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body["req"])
        return httpx.Response(200, json=[])

    c = client_with(handler)
    now = 10**12
    await c.candles("BTC", "1m", 0, now)
    assert seen["startTime"] == now - 60_000 * 5000
    await c.aclose()


async def test_limiter_waits_when_budget_is_spent():
    clock = {"t": 0.0}
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        clock["t"] += s

    lim = WeightLimiter(60, clock=lambda: clock["t"], sleep=fake_sleep)
    await lim.acquire(40)
    clock["t"] = 30.0
    await lim.acquire(20)  # fits: 60 in the window
    assert not slept
    await lim.acquire(20)  # waits until the first 40 leave the window (t = 60)
    assert sum(slept) == pytest.approx(30.0)
    lim.charge(50)
    assert lim.available == pytest.approx(60 - 20 - 20 - 50)


async def test_limiter_never_exceeds_budget_in_any_minute_including_the_first():
    clock = {"t": 0.0}
    booked: list[tuple[float, float]] = []

    async def fake_sleep(s: float) -> None:
        clock["t"] += s

    lim = WeightLimiter(1000, clock=lambda: clock["t"], sleep=fake_sleep)
    for _ in range(300):
        t = await lim.acquire(20, reserve=100)  # a fills page: base 20 + up to 100 per-item surcharge
        booked.append((clock["t"], 0.0))
        lim.settle(t, 100, 60)
        booked[-1] = (clock["t"], 80.0)
        clock["t"] += 0.05
    times = [b[0] for b in booked]
    for i, t0 in enumerate(times):
        in_window = sum(w for t, w in booked[i:] if t < t0 + 60)
        assert in_window <= 1000 + 1e-6
    assert lim.spent == pytest.approx(300 * 80)

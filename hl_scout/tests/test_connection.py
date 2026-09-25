"""Connection layer: preflight, circuit breaker, WebSocket keep-alive/acks/limits, read-only account view."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from hl_scout.accounts import AddressError, account_warnings, parse_account, render_account, resolve_role
from hl_scout.cli import main
from hl_scout.config import ApiCfg
from hl_scout.hl.client import ConnectivityError, InfoClient, gather_limited
from hl_scout.hl.ws import StaleConnection, WsSession, sub_key, validate_subscriptions
from hl_scout.util import DAY

A = "0x" + "a" * 40
M = "0x" + "b" * 40


async def no_sleep(_: float) -> None:
    return None


def client_with(handler, **cfg) -> InfoClient:
    api = ApiCfg(**{"retries": 3, "backoff_base_s": 0.0, **cfg})
    return InfoClient(api, transport=httpx.MockTransport(handler), sleep=no_sleep)


# --- REST -------------------------------------------------------------------------------------------------


async def test_preflight_ok_and_blocked():
    ok = client_with(lambda r: httpx.Response(200, json={"BTC": "60000", "ETH": "3000"}))
    assert await ok.preflight() == 2
    await ok.aclose()

    def blocked(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("CONNECT tunnel failed, response 403")

    bad = client_with(blocked)
    with pytest.raises(ConnectivityError, match="403"):
        await bad.preflight()
    await bad.aclose()


async def test_circuit_breaker_stops_retrying_a_dead_network():
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("name resolution failed")

    c = client_with(down, retries=5, max_consecutive_failures=3)
    with pytest.raises(ConnectivityError):
        await c.info({"type": "meta"})
    assert calls["n"] == 3
    with pytest.raises(ConnectivityError):  # further calls fail fast, without touching the network
        await c.info({"type": "meta"})
    assert calls["n"] == 3
    await c.aclose()


async def test_breaker_half_opens_after_cooldown():
    state = {"up": False}

    def flaky(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": 1})

    c = client_with(flaky, retries=5, max_consecutive_failures=2)
    with pytest.raises(ConnectivityError):
        await c.info({"type": "meta"})
    state["up"] = True
    c._tripped_at = time.monotonic() - c.breaker_cooldown_s - 1  # cooldown elapsed
    assert await c.info({"type": "meta"}) == {"ok": 1}
    await c.aclose()


async def test_gather_limited_raises_only_fatal_errors():
    async def ok():
        return 1

    async def bad_address():
        raise ValueError("one bad address")

    async def api_down():
        raise ConnectivityError("down")

    res = await gather_limited([ok(), bad_address()], 2)
    assert res[0] == 1 and isinstance(res[1], ValueError)
    with pytest.raises(ConnectivityError):
        await gather_limited([ok(), api_down()], 2)


# --- WebSocket ---------------------------------------------------------------------------------------------


class FakeWs:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        if msg.get("method") == "subscribe" and self.server.ack:
            await self.inbox.put(
                json.dumps(
                    {
                        "channel": "subscriptionResponse",
                        "data": {"method": "subscribe", "subscription": msg["subscription"]},
                    }
                )
            )
        if msg.get("method") == "ping" and self.server.pong(self):
            await self.inbox.put(json.dumps({"channel": "pong"}))

    async def recv(self) -> str:
        return await self.inbox.get()

    async def __aenter__(self) -> FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeServer:
    """First connection goes half-open (no pong, no data); later connections behave."""

    def __init__(self, ack: bool = True, half_open_first: bool = True) -> None:
        self.ack = ack
        self.half_open_first = half_open_first
        self.sockets: list[FakeWs] = []

    def pong(self, ws: FakeWs) -> bool:
        return not (self.half_open_first and ws is self.sockets[0])

    def connect(self, url: str, **kw: object) -> FakeWs:
        ws = FakeWs(self)
        self.sockets.append(ws)
        if len(self.sockets) > 1 or not self.half_open_first:
            ws.inbox.put_nowait(json.dumps({"channel": "trades", "data": [{"coin": "BTC", "px": "1", "sz": "1"}]}))
            ws.inbox.put_nowait(json.dumps({"channel": "error", "data": "Invalid subscription"}))
        return ws


async def test_ws_tracks_acks_and_yields_data():
    server = FakeServer(half_open_first=False)
    subs = [{"type": "trades", "coin": "BTC"}, {"type": "trades", "coin": "ETH"}]
    s = WsSession("wss://x", subs, connect=server.connect, ping_every_s=0.05, pong_timeout_s=0.05)
    loop = asyncio.get_running_loop()
    got = [m async for m in s.messages(loop.time() + 0.3)]
    assert [m["channel"] for m in got] == ["trades"]  # acks, pongs and errors are not data
    assert s.all_acked and s.status.reconnects == 0
    assert s.status.errors == ["Invalid subscription"]


async def test_ws_reconnects_a_half_open_socket_and_resubscribes():
    server = FakeServer(half_open_first=True)
    gaps: list[tuple[float, float]] = []

    async def on_reconnect(a: float, b: float) -> None:
        gaps.append((a, b))

    subs = [{"type": "userFills", "user": A}]
    s = WsSession(
        "wss://x",
        subs,
        connect=server.connect,
        ping_every_s=0.05,
        pong_timeout_s=0.05,
        max_backoff_s=0.01,
        on_reconnect=on_reconnect,
    )
    loop = asyncio.get_running_loop()
    got = [m async for m in s.messages(loop.time() + 0.6)]
    assert len(server.sockets) >= 2 and s.status.reconnects >= 1
    assert gaps and gaps[0][0] < gaps[0][1]
    for ws in server.sockets:  # every connection re-sent the subscription
        assert {"method": "subscribe", "subscription": subs[0]} in ws.sent
    assert got and got[0]["channel"] == "trades"
    assert StaleConnection.__name__ == "StaleConnection"


def test_ws_limits():
    validate_subscriptions([{"type": "userFills", "user": f"0x{i:040x}"} for i in range(10)])
    with pytest.raises(ValueError, match="10"):
        validate_subscriptions([{"type": "userFills", "user": f"0x{i:040x}"} for i in range(11)])
    with pytest.raises(ValueError, match="1000"):
        validate_subscriptions([{"type": "trades", "coin": str(i)} for i in range(1001)])
    assert sub_key({"type": "userFills", "user": A.upper()}) == sub_key({"user": A, "type": "userFills"})


# --- read-only account ----------------------------------------------------------------------------------------


def test_resolve_role():
    assert resolve_role(A, {"role": "user"}).address == A
    agent = resolve_role(A, {"role": "agent", "data": {"user": M}})
    assert agent.address == M and "агент" in (agent.note or "")
    sub = resolve_role(A, {"role": "subAccount", "data": {"master": M}})
    assert sub.address == A and sub.master == M
    with pytest.raises(AddressError):
        resolve_role(A, {"role": "missing"})
    with pytest.raises(ValueError):
        resolve_role("0x123", {"role": "user"})


def test_parse_account_and_warnings():
    now = 1_790_000_000_000
    ch = {
        "marginSummary": {"accountValue": "48.5", "totalMarginUsed": "42", "totalNtlPos": "120"},
        "withdrawable": "6.5",
        "assetPositions": [
            {
                "type": "oneWay",
                "position": {
                    "coin": "ETH",
                    "szi": "0.04",
                    "entryPx": "3000",
                    "positionValue": "118",
                    "unrealizedPnl": "-2",
                    "liquidationPx": "2860",
                    "leverage": {"type": "cross", "value": 3},
                },
            },
        ],
    }
    spot = {"balances": [{"coin": "USDC", "total": "1.5"}]}
    agents = [
        {"name": "copybot", "address": M, "validUntil": now + 3 * DAY},
        {"name": "old", "address": A, "validUntil": None},
    ]
    snap = parse_account(A, ch, spot, {"ETH": "2950"}, agents, now)
    assert snap.positions[0].liq_distance == pytest.approx((2950 - 2860) / 2950)
    warns = account_warnings(snap)
    assert any("ETH" in w for w in warns)
    assert any("copybot" in w and "истекает" in w for w in warns)
    assert any("маржи" in w for w in warns)
    text = render_account(snap)
    assert "ETH лонг" in text and "бессрочно" in text


def test_cli_rejects_bad_address_without_network(capsys):
    assert main(["check", "0x123"]) == 3
    assert "не адрес" in capsys.readouterr().out

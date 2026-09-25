"""Market WS pool and Sports WS against local fake servers (no external network)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from websockets.asyncio.server import Server, ServerConnection, serve

from polybot.core.config import MarketWsConfig, SportsWsConfig
from polybot.data.records import Kind, Source
from polybot.venues.polymarket.clob_ws import MarketPool
from polybot.venues.polymarket.orderbook import BookTracker
from polybot.venues.polymarket.sports_ws import SportsFeed
from tests.conftest import ListWriter


async def eventually(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def book(asset: str) -> dict[str, Any]:
    return {
        "event_type": "book",
        "market": "0x" + "01" * 32,
        "asset_id": asset,
        "bids": [{"price": "0.48", "size": "10"}],
        "asks": [{"price": "0.52", "size": "10"}],
        "hash": f"h-{asset}",
        "timestamp": "1787015700000",
    }


class FakeMarketServer:
    """Speaks the market channel protocol (docs/api_notes.md §10)."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, dict[str, Any]]] = []
        self.connections: list[ServerConnection] = []
        self.server: Server | None = None

    async def handler(self, ws: ServerConnection) -> None:
        index = len(self.connections)
        self.connections.append(ws)
        async for raw in ws:
            if raw == "PING":
                await ws.send("PONG")
                continue
            msg = json.loads(raw)
            self.messages.append((index, msg))
            if msg.get("type") == "market" or msg.get("operation") == "subscribe":
                await ws.send(json.dumps([book(a) for a in msg["assets_ids"]]))

    @property
    def url(self) -> str:
        assert self.server is not None
        port = next(iter(self.server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}"


@pytest.fixture
async def market_server() -> AsyncIterator[FakeMarketServer]:
    fake = FakeMarketServer()
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        fake.server = server
        yield fake


def pool_cfg(**overrides: object) -> MarketWsConfig:
    base: dict[str, object] = {
        "max_assets_per_conn": 2,
        "ping_interval_s": 0.1,
        "stale_after_s": 2.0,
        "snapshot_timeout_s": 0.5,
        "resync_min_interval_s": 0.0,
    }
    base.update(overrides)
    return MarketWsConfig.model_validate(base)


async def test_shards_subscribes_and_gets_snapshots(market_server: FakeMarketServer) -> None:
    writer = ListWriter()
    pool = MarketPool(pool_cfg(), market_server.url, writer, BookTracker())  # type: ignore[arg-type]
    try:
        await pool.set_assets({"a1", "a2", "a3"})
        await eventually(lambda: all(pool.tracker.books[a].ready for a in ("a1", "a2", "a3")))
        initial = [m for _, m in market_server.messages if m.get("type") == "market"]
        assert sorted(len(m["assets_ids"]) for m in initial) == [1, 2]
        assert all(m["custom_feature_enabled"] is False for m in initial)  # M1 default
        frames = writer.of(Source.CLOB_MARKET_WS, Kind.FRAME)
        assert frames and all(f.ts_recv_ns > 0 and f.conn_id for f in frames)
        assert frames[0].event_type == "book" and frames[0].n_events in (1, 2)
        await eventually(lambda: bool(writer.of(Source.CLOB_MARKET_WS, Kind.PROBE)))
        assert pool.health()["awaiting_snapshot"] == 0
    finally:
        await pool.close()


async def test_desync_triggers_resubscribe(market_server: FakeMarketServer) -> None:
    writer = ListWriter()
    pool = MarketPool(pool_cfg(), market_server.url, writer, BookTracker())  # type: ignore[arg-type]
    try:
        await pool.set_assets({"a1"})
        await eventually(lambda: pool.tracker.books["a1"].ready)
        wrong_top = {
            "event_type": "price_change",
            "market": "0x" + "01" * 32,
            "price_changes": [
                {
                    "asset_id": "a1",
                    "price": "0.49",
                    "size": "5",
                    "side": "BUY",
                    "best_bid": "0.50",
                    "best_ask": "0.52",
                }
            ],
        }
        await market_server.connections[0].send(json.dumps(wrong_top))
        await eventually(
            lambda: any(m.get("operation") == "unsubscribe" for _, m in market_server.messages)
        )
        await eventually(lambda: pool.tracker.books["a1"].ready)
        ops = [m.get("operation") for _, m in market_server.messages]
        assert ops[-2:] == ["unsubscribe", "subscribe"]
        desyncs = [
            r for r in writer.of(Source.CLOB_MARKET_WS, Kind.CONTROL) if r.event_type == "desync"
        ]
        assert desyncs and json.loads(desyncs[0].payload)["reason"] == "top_mismatch"
        assert pool.stats.resyncs == 1
    finally:
        await pool.close()


async def test_reconnect_replays_subscription(market_server: FakeMarketServer) -> None:
    writer = ListWriter()
    pool = MarketPool(pool_cfg(), market_server.url, writer, BookTracker())  # type: ignore[arg-type]
    try:
        await pool.set_assets({"a1", "a2"})
        await eventually(
            lambda: len(market_server.connections) == 1 and pool.tracker.books["a2"].ready
        )
        await market_server.connections[0].close()
        await eventually(lambda: len(market_server.connections) == 2)
        await eventually(lambda: pool.tracker.books["a1"].ready and pool.tracker.books["a2"].ready)
        replayed = [m for i, m in market_server.messages if i == 1 and m.get("type") == "market"]
        assert replayed and sorted(replayed[0]["assets_ids"]) == ["a1", "a2"]
        controls = [r.event_type for r in writer.of(Source.CLOB_MARKET_WS, Kind.CONTROL)]
        assert controls.count("connected") == 2 and "disconnected" in controls
    finally:
        await pool.close()


async def test_removal_unsubscribes_and_closes_empty_connection(
    market_server: FakeMarketServer,
) -> None:
    pool = MarketPool(pool_cfg(), market_server.url, ListWriter(), BookTracker())  # type: ignore[arg-type]
    try:
        await pool.set_assets({"a1", "a2", "a3"})
        await eventually(lambda: all(pool.tracker.books[a].ready for a in ("a1", "a2", "a3")))
        await pool.set_assets({"a1"})
        assert set(pool.assets) == {"a1"}
        assert pool.health()["conns"] == 1
        await eventually(
            lambda: any(m.get("operation") == "unsubscribe" for _, m in market_server.messages)
        )
    finally:
        await pool.close()


async def test_sports_feed_answers_ping_and_records_games() -> None:
    received: list[str] = []

    async def handler(ws: ServerConnection) -> None:
        await ws.send("ping")
        received.append(str(await ws.recv()))
        await ws.send(
            json.dumps(
                {
                    "gameId": 90001,
                    "leagueAbbreviation": "atp",
                    "status": "InProgress",
                    "live": True,
                    "ended": False,
                    "score": "6-4, 2-1",
                }
            )
        )
        await asyncio.sleep(1)

    writer = ListWriter()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        feed = SportsFeed(SportsWsConfig(), f"ws://127.0.0.1:{port}", writer)  # type: ignore[arg-type]
        task = asyncio.create_task(feed.run())
        try:
            await eventually(lambda: bool(writer.of(Source.SPORTS_WS, Kind.FRAME)))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert received == ["pong"]
    (frame,) = writer.of(Source.SPORTS_WS, Kind.FRAME)
    assert frame.key == "90001" and frame.event_type == "InProgress"
    assert feed.live_games == {"90001"}

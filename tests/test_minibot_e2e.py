"""The whole mini-bot against a local fake Polymarket (Gamma, geoblock, market WS) and Telegram.

No external network: HTTP goes through httpx.MockTransport, the market channel is a local
WebSocket server. Checks the path start → selection → books → paper quotes → fills →
status and state files → Parquet records → Telegram start/stop messages.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
from websockets.asyncio.server import ServerConnection, serve

from polybot.core.config import BaseConfig, Settings
from polybot.core.timeutil import now_ns
from polybot.data.sink import ParquetSink
from polybot.minibot.app import RAW_DIR, REPORTS_DIR, _run
from polybot.minibot.engine import STATE_FILE, STATUS_FILE
from tests.minibot_helpers import H, book_event, mini_config, raw_event, trade_event
from tests.test_ws_pool import eventually

TOKEN = "123456789:AAH-e2e-token-value_for_tests-xyz"
INTER = "100001"


class FakeMarketChannel:
    """Snapshots on subscribe, then a trade through the bid every 0.3 s (docs/api_notes.md §10)."""

    def __init__(self) -> None:
        self.subscribed: set[str] = set()

    async def handler(self, ws: ServerConnection) -> None:
        async def trades() -> None:
            while True:
                await asyncio.sleep(0.3)
                if INTER in self.subscribed:
                    await ws.send(json.dumps(trade_event(INTER, "0.38", "30")))

        feeder = asyncio.create_task(trades())
        try:
            async for raw in ws:
                if raw == "PING":
                    await ws.send("PONG")
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "market" or msg.get("operation") == "subscribe":
                    assets = [str(a) for a in msg["assets_ids"]]
                    self.subscribed.update(assets)
                    books = [
                        book_event(a, [("0.40", "300"), ("0.39", "500")], [("0.44", "300")])
                        for a in assets
                    ]
                    await ws.send(json.dumps(books))
        finally:
            feeder.cancel()


def fake_http(event: dict[str, Any], telegram: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "polymarket.com" and url.path == "/api/geoblock":
            return httpx.Response(200, json={"blocked": False, "country": "AM", "ip": "1.2.3.4"})
        if url.host == "api.telegram.org":
            assert url.path == f"/bot{TOKEN}/sendMessage"
            telegram.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if url.path == "/tags/slug/serie-a":
            return httpx.Response(200, json={"id": "7", "slug": "serie-a"})
        if url.path == "/events/keyset":
            assert url.params.get("tag_id") == "7"
            return httpx.Response(200, json={"events": [event]})
        if url.path.startswith("/markets/"):
            return httpx.Response(200, json={"closed": False})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


async def test_minibot_end_to_end(tmp_path: Path) -> None:
    channel = FakeMarketChannel()
    telegram: list[dict[str, Any]] = []
    event = raw_event("1000", now_ns() + 5 * H)
    async with serve(channel.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        base = BaseConfig.model_validate(
            {
                "geoblock": {"allowed_countries": ["AM"]},
                "polymarket": {"market_ws_url": f"ws://127.0.0.1:{port}"},
            }
        )
        cfg = mini_config(
            selection={"refresh_s": 1},
            status_interval_s=0.2,
            telegram={"status_every_h": 0},
            sink={"flush_interval_s": 0.2},
        )
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            DATA_DIR=str(tmp_path),
            TELEGRAM_BOT_TOKEN=TOKEN,
            TELEGRAM_ALLOWED_CHAT_IDS="42",
        )
        sink = ParquetSink(tmp_path / RAW_DIR, flush_interval_s=0.2)
        sink_task = asyncio.create_task(sink.run())
        stop = asyncio.Event()
        async with httpx.AsyncClient(transport=fake_http(event, telegram)) as http:
            bot = asyncio.create_task(_run(settings, base, cfg, sink, http, stop=stop))
            status_path = tmp_path / "state" / STATUS_FILE

            def filled() -> bool:
                try:
                    return bool(json.loads(status_path.read_text(encoding="utf-8"))["fills_day"])
                except (OSError, ValueError, KeyError):
                    return False

            await eventually(filled, timeout=15.0)
            status = json.loads(status_path.read_text(encoding="utf-8"))
            stop.set()
            assert await asyncio.wait_for(bot, 10.0) == 0
        await sink.close()  # after the flusher's current write, then the rest
        sink_task.cancel()  # the flusher has returned: a no-op safety net
        await asyncio.gather(sink_task, return_exceptions=True)

    # Status file: the Yes tokens of the three markets, Inter quoted, the rest without books.
    by_token = {m["token"]: m for m in status["markets"]}
    assert set(by_token) == {"100001", "100011", "100021"}
    assert by_token[INTER]["bid"] and status["market_ws"]["conns_open"] == 1
    assert channel.subscribed == {"100001", "100011", "100021"}  # never the No tokens
    # State: the position survives, open orders do not.
    state = json.loads((tmp_path / "state" / STATE_FILE).read_text(encoding="utf-8"))
    assert float(state["holdings"][INTER]["long"]) >= 20 and state["day"]["fills"] >= 1
    # Parquet: paper orders and fills next to the raw frames.
    table = pq.read_table(tmp_path / RAW_DIR).to_pylist()
    paper = [r["event_type"] for r in table if r["kind"] == "control" and r["asset_id"] == INTER]
    assert "order" in paper and "fill" in paper
    assert any(r["event_type"] == "book" for r in table)
    # Telegram: start and stop, HTML, to the configured chat only.
    texts = [m["text"] for m in telegram]
    assert "Мини-бот запущен" in texts[0] and "Мини-бот остановлен" in texts[-1]
    assert {m["chat_id"] for m in telegram} == {42}
    assert (tmp_path / REPORTS_DIR / "rules_templates.md").exists()

"""Minimal WebSocket reader for public channels [api_notes §5].

Used by discovery to collect addresses from large trades (`trades` channel carries `users: [buyer, seller]`).
The monitor (next stage) will reuse `WsSession` for `userFills`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import websockets

from hl_scout.log import get_logger

log = get_logger(__name__)

PING_EVERY_S = 50.0  # the official SDK pings every 50 s [api_notes §5]
MAX_SUBSCRIPTIONS = 1000  # [api_notes §4]


@dataclass(frozen=True)
class LargeTrade:
    time: int
    coin: str
    px: float
    sz: float
    notional: float
    buyer: str
    seller: str
    hash: str


class WsSession:
    """One connection, re-subscribes after reconnect, yields decoded messages until `deadline` (loop time)."""

    def __init__(self, url: str, subscriptions: list[dict[str, Any]], *, connect: Callable[..., Any] | None = None):
        if len(subscriptions) > MAX_SUBSCRIPTIONS:
            raise ValueError("слишком много подписок для одного IP")
        self.url = url
        self.subscriptions = subscriptions
        self._connect = connect or websockets.connect

    async def messages(self, deadline: float) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        backoff = 1.0
        while loop.time() < deadline:
            try:
                async with self._connect(self.url, ping_interval=None, max_size=None) as ws:
                    for sub in self.subscriptions:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    backoff, last_ping = 1.0, loop.time()
                    while loop.time() < deadline:
                        wait = max(0.1, min(deadline - loop.time(), PING_EVERY_S - (loop.time() - last_ping)))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                        except TimeoutError:
                            raw = None
                        if loop.time() - last_ping >= PING_EVERY_S:
                            await ws.send(json.dumps({"method": "ping"}))
                            last_ping = loop.time()
                        if raw is None:
                            continue
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue  # e.g. "Websocket connection established."
                        if isinstance(msg, dict) and msg.get("channel") not in (None, "pong", "subscriptionResponse"):
                            yield msg
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                log.warning("ws_reconnect", url=self.url, err=str(exc), backoff_s=backoff)
                await asyncio.sleep(min(backoff, max(0.0, deadline - loop.time())))
                backoff = min(backoff * 2, 60.0)


def parse_large_trades(msg: dict[str, Any], min_notional: float) -> list[LargeTrade]:
    if msg.get("channel") != "trades":
        return []
    out: list[LargeTrade] = []
    for t in msg.get("data") or []:
        users = t.get("users")
        if not users or len(users) != 2:
            continue
        try:
            px, sz = float(t["px"]), float(t["sz"])
        except (KeyError, TypeError, ValueError):
            continue
        notional = px * sz
        if notional >= min_notional:
            out.append(
                LargeTrade(
                    time=int(t.get("time") or 0),
                    coin=str(t.get("coin")),
                    px=px,
                    sz=sz,
                    notional=notional,
                    buyer=str(users[0]).lower(),
                    seller=str(users[1]).lower(),
                    hash=str(t.get("hash") or ""),
                )
            )
    return out


async def collect_large_trades(
    url: str, coins: list[str], min_notional: float, duration_s: float, *, connect: Callable[..., Any] | None = None
) -> list[LargeTrade]:
    session = WsSession(url, [{"type": "trades", "coin": c} for c in coins], connect=connect)
    deadline = asyncio.get_running_loop().time() + duration_s
    found: list[LargeTrade] = []
    async for msg in session.messages(deadline):
        found.extend(parse_large_trades(msg, min_notional))
    log.info("large_trades_collected", coins=len(coins), trades=len(found), minutes=round(duration_s / 60, 1))
    return found

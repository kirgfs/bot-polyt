"""WebSocket session for public channels [api_notes §5].

Discovery uses it for large trades (`trades.users`); the monitor (next stage) will use it for `userFills`.

Connection hygiene (see api_notes §5 and §10):
- the server drops a connection that stays silent ~60 s → ping every `ping_every_s` (30 s);
- a ping without any answer within `pong_timeout_s` (10 s) means a half-open socket → reconnect;
- every (re)connect re-sends all subscriptions and tracks `subscriptionResponse` acknowledgements;
- reconnects never give up before the deadline (backoff with jitter, capped);
- `on_reconnect(gap_start, gap_end)` lets the caller backfill what the gap missed via REST;
- limits: ≤ 1000 subscriptions, ≤ 10 distinct users in user-specific subscriptions [api_notes §4].
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import websockets

from hl_scout.log import get_logger

log = get_logger(__name__)

MAX_SUBSCRIPTIONS = 1000  # [api_notes §4]
MAX_USERS = 10  # distinct users across user-specific subscriptions [api_notes §4]


class StaleConnection(Exception):
    """No pong (or any frame) arrived in time after a ping: the socket is half-open."""


def sub_key(sub: dict[str, Any]) -> str:
    """Canonical form of a subscription, to match server acknowledgements (which echo it back)."""
    norm = {k: (v.lower() if k == "user" and isinstance(v, str) else v) for k, v in sub.items()}
    return json.dumps(norm, sort_keys=True, separators=(",", ":"))


def validate_subscriptions(subs: list[dict[str, Any]]) -> None:
    if len(subs) > MAX_SUBSCRIPTIONS:
        raise ValueError(f"{len(subs)} подписок > лимита {MAX_SUBSCRIPTIONS} на IP")
    users = {str(s["user"]).lower() for s in subs if "user" in s}
    if len(users) > MAX_USERS:
        raise ValueError(f"{len(users)} разных адресов в пользовательских подписках > лимита {MAX_USERS} на IP")


@dataclass
class WsStatus:
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    last_frame_at: float | None = None  # loop time
    acked: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def age_s(self, now: float) -> float | None:
        return None if self.last_frame_at is None else now - self.last_frame_at


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
    """One logical stream over reconnecting sockets; yields data frames until `deadline` (loop time)."""

    def __init__(
        self,
        url: str,
        subscriptions: list[dict[str, Any]],
        *,
        connect: Callable[..., Any] | None = None,
        ping_every_s: float = 30.0,
        pong_timeout_s: float = 10.0,
        max_backoff_s: float = 60.0,
        on_reconnect: Callable[[float, float], Awaitable[None]] | None = None,
    ) -> None:
        validate_subscriptions(subscriptions)
        self.url = url
        self.subscriptions = subscriptions
        self._connect = connect or websockets.connect
        self.ping_every_s = ping_every_s
        self.pong_timeout_s = pong_timeout_s
        self.max_backoff_s = max_backoff_s
        self.on_reconnect = on_reconnect
        self.status = WsStatus()

    @property
    def all_acked(self) -> bool:
        return {sub_key(s) for s in self.subscriptions} <= self.status.acked

    async def messages(self, deadline: float) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        backoff = 1.0
        gap_start: float | None = None
        while loop.time() < deadline:
            try:
                async with self._connect(self.url, ping_interval=None, max_size=None) as ws:
                    st = self.status
                    st.connected, st.connects = True, st.connects + 1
                    st.acked.clear()
                    for sub in self.subscriptions:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    if gap_start is not None and self.on_reconnect is not None:
                        await self.on_reconnect(gap_start, loop.time())
                    gap_start, backoff = None, 1.0
                    try:
                        async for msg in self._read(ws, deadline):
                            yield msg
                    finally:
                        st.connected = False
                    return  # deadline reached on a healthy connection
            except (OSError, websockets.WebSocketException, TimeoutError, StaleConnection) as exc:
                self.status.connected = False
                self.status.reconnects += 1
                if gap_start is None:  # first failure of this outage: data is missing from here on
                    gap_start = loop.time()
                delay = min(backoff, self.max_backoff_s) * (0.5 + random.random() / 2)
                log.warning(
                    "ws_reconnect", url=self.url, err=str(exc) or exc.__class__.__name__, backoff_s=round(delay, 2)
                )
                await asyncio.sleep(min(delay, max(0.0, deadline - loop.time())))
                backoff = min(backoff * 2, self.max_backoff_s)

    async def _read(self, ws: Any, deadline: float) -> AsyncIterator[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        st = self.status
        st.last_frame_at = loop.time()
        next_ping = loop.time() + self.ping_every_s
        ping_sent_at: float | None = None
        while (now := loop.time()) < deadline:
            wait = min(deadline - now, next_ping - now)
            if ping_sent_at is not None:
                wait = min(wait, ping_sent_at + self.pong_timeout_s - now)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.01, wait))
            except TimeoutError:
                raw = None
            now = loop.time()
            if raw is not None:
                st.last_frame_at = now
                ping_sent_at = None  # any frame proves the socket is alive
                msg = _decode(raw)
                if msg is not None:
                    channel = msg.get("channel")
                    if channel == "subscriptionResponse":
                        sub = (msg.get("data") or {}).get("subscription")
                        if isinstance(sub, dict):
                            st.acked.add(sub_key(sub))
                    elif channel == "error":
                        st.errors.append(str(msg.get("data")))
                        log.warning("ws_server_error", data=str(msg.get("data"))[:300])
                    elif channel not in (None, "pong"):
                        yield msg
            if ping_sent_at is not None and now - ping_sent_at >= self.pong_timeout_s:
                raise StaleConnection(f"нет ответа на ping {self.pong_timeout_s:g} с")
            if now >= next_ping:
                await ws.send(json.dumps({"method": "ping"}))
                ping_sent_at, next_ping = now, now + self.ping_every_s


def _decode(raw: Any) -> dict[str, Any] | None:
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return None  # e.g. "Websocket connection established."
    return msg if isinstance(msg, dict) else None


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
    url: str,
    coins: list[str],
    min_notional: float,
    duration_s: float,
    *,
    connect: Callable[..., Any] | None = None,
    ping_every_s: float = 30.0,
    pong_timeout_s: float = 10.0,
) -> list[LargeTrade]:
    session = WsSession(
        url,
        [{"type": "trades", "coin": c} for c in coins],
        connect=connect,
        ping_every_s=ping_every_s,
        pong_timeout_s=pong_timeout_s,
    )
    deadline = asyncio.get_running_loop().time() + duration_s
    found: list[LargeTrade] = []
    async for msg in session.messages(deadline):
        found.extend(parse_large_trades(msg, min_notional))
    st = session.status
    log.info(
        "large_trades_collected",
        coins=len(coins),
        trades=len(found),
        minutes=round(duration_s / 60, 1),
        reconnects=st.reconnects,
        acked=len(st.acked),
        errors=len(st.errors),
    )
    return found

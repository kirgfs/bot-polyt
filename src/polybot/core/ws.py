"""Thin reconnecting WebSocket client that hands raw frames to a callback.

Own client instead of the SDK streams (CLAUDE.md): the recorder needs every raw frame
with its receive time, and explicit control over heartbeat, reconnect and resubscribe.

Heartbeat modes (docs/api_notes.md §10), liveness as in the SDK:
- CLIENT_PING: we send "PING" every interval, the server answers "PONG" (CLOB market and
  user channels); stale if no PONG within `stale_after_s`. PING→PONG round trips are
  reported as latency samples;
- SERVER_PING: the server sends "ping", we answer "pong" (Sports WS); stale if no ping;
- NONE: no app-level heartbeat; stale if no frame at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns

log = get_logger(__name__)

# SDK reconnect defaults: jittered exponential backoff, 0.25 s base, 30 s cap
# ([SDK] _internal/ws/backoff.py).
BACKOFF_BASE_S = 0.25
BACKOFF_MAX_S = 30.0
# A connection that lived this long resets the backoff.
STABLE_AFTER_S = 60.0


class Heartbeat(StrEnum):
    CLIENT_PING = "client_ping"
    SERVER_PING = "server_ping"
    NONE = "none"


class ControlEvent(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    STALE = "stale"
    CONNECT_FAILED = "connect_failed"
    UNDECODABLE = "undecodable_frame"
    HANDLER_ERROR = "handler_error"


FrameHandler = Callable[[str, int, "WsConnection"], None]
OpenHandler = Callable[["WsConnection"], Awaitable[None]]
ControlHandler = Callable[[ControlEvent, dict[str, object], "WsConnection"], None]
RttHandler = Callable[[int, "WsConnection"], None]


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    cap = min(BACKOFF_BASE_S * float(2 ** min(attempt, 16)), BACKOFF_MAX_S)
    draw: float = rng.random() if rng is not None else random.random()  # noqa: S311 - jitter
    return draw * cap


@dataclass
class WsStats:
    frames: int = 0
    bytes: int = 0
    connects: int = 0
    disconnects: int = 0
    stale_closes: int = 0
    connect_failures: int = 0
    handler_errors: int = 0
    last_frame_ns: int = 0
    connected_since_ns: int = 0
    rtt_samples: deque[int] = field(default_factory=lambda: deque(maxlen=512))


class WsConnection:
    def __init__(
        self,
        *,
        name: str,
        url: str,
        heartbeat: Heartbeat,
        on_frame: FrameHandler,
        on_open: OpenHandler | None = None,
        on_control: ControlHandler | None = None,
        on_rtt: RttHandler | None = None,
        ping_interval_s: float = 10.0,
        stale_after_s: float = 30.0,
        open_timeout_s: float = 10.0,
        max_frame_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.name = name
        self.url = url
        self._heartbeat = heartbeat
        self._on_frame = on_frame
        self._on_open = on_open
        self._on_control = on_control
        self._on_rtt = on_rtt
        self._ping_interval_s = ping_interval_s
        self._stale_after_s = stale_after_s
        self._open_timeout_s = open_timeout_s
        self._max_frame_bytes = max_frame_bytes
        self._ws: ClientConnection | None = None
        self._generation = 0
        self._pings: deque[int] = deque()
        self._last_alive_mono = 0
        self.stats = WsStats()

    @property
    def conn_id(self) -> str:
        return f"{self.name}#{self._generation}"

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    async def send(self, text: str) -> bool:
        """Best effort: False if not connected; the next (re)connect replays state via on_open."""
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(text)
        except ConnectionClosed:
            return False
        return True

    async def close(self) -> None:
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def _control(self, event: ControlEvent, **info: object) -> None:
        if self._on_control is not None:
            self._on_control(event, info, self)

    async def run(self) -> None:
        """Connect forever. Cancel the task to stop."""
        attempt = 0
        while True:
            try:
                ws = await connect(
                    self.url,
                    open_timeout=self._open_timeout_s,
                    close_timeout=2,
                    max_size=self._max_frame_bytes,
                )
            except (OSError, TimeoutError, WebSocketException) as exc:
                self.stats.connect_failures += 1
                self._control(ControlEvent.CONNECT_FAILED, error=type(exc).__name__)
                log.warning("ws_connect_failed", conn=self.name, error=repr(exc))
                await asyncio.sleep(backoff_delay(attempt))
                attempt += 1
                continue
            started = mono_ns()
            try:
                await self._session(ws)
            except Exception:
                log.exception("ws_session_crashed", conn=self.conn_id)
            lived_s = (mono_ns() - started) / NS_PER_S
            attempt = 0 if lived_s >= STABLE_AFTER_S else attempt + 1
            await asyncio.sleep(backoff_delay(attempt))

    async def _session(self, ws: ClientConnection) -> None:
        self._generation += 1
        self._ws = ws
        self._pings.clear()
        self._last_alive_mono = mono_ns()
        self.stats.connects += 1
        self.stats.connected_since_ns = now_ns()
        self._control(ControlEvent.CONNECTED)
        log.info("ws_connected", conn=self.conn_id)
        tasks = [asyncio.create_task(self._reader(ws)), asyncio.create_task(self._watchdog(ws))]
        if self._heartbeat is Heartbeat.CLIENT_PING:
            tasks.append(asyncio.create_task(self._pinger(ws)))
        try:
            if self._on_open is not None:
                await self._on_open(self)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, ConnectionClosed):
                    log.warning("ws_session_task_failed", conn=self.conn_id, error=repr(exc))
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await ws.close()
            self._ws = None
            self.stats.disconnects += 1
            self.stats.connected_since_ns = 0
            self._control(ControlEvent.DISCONNECTED, code=ws.close_code, reason=ws.close_reason)
            log.info("ws_disconnected", conn=self.conn_id, code=ws.close_code)

    async def _reader(self, ws: ClientConnection) -> None:
        async for message in ws:
            ts_recv = now_ns()
            if isinstance(message, bytes):
                try:
                    text = message.decode("utf-8")
                except UnicodeDecodeError:
                    self._control(ControlEvent.UNDECODABLE, size=len(message))
                    continue
            else:
                text = message
            if self._heartbeat is Heartbeat.CLIENT_PING and text == "PONG":
                self._on_pong()
                continue
            if self._heartbeat is Heartbeat.SERVER_PING and text == "ping":
                self._last_alive_mono = mono_ns()
                await ws.send("pong")
                continue
            if self._heartbeat is Heartbeat.NONE:
                self._last_alive_mono = mono_ns()
            self.stats.frames += 1
            self.stats.bytes += len(text)
            self.stats.last_frame_ns = ts_recv
            try:
                self._on_frame(text, ts_recv, self)
            except Exception as exc:
                # A parsing bug must not tear down the feed, but it must not be silent.
                self.stats.handler_errors += 1
                if self.stats.handler_errors <= 10 or self.stats.handler_errors % 1000 == 0:
                    log.exception("ws_frame_handler_failed", conn=self.conn_id)
                    self._control(ControlEvent.HANDLER_ERROR, error=type(exc).__name__)

    def _on_pong(self) -> None:
        now = mono_ns()
        self._last_alive_mono = now
        if not self._pings:
            return
        rtt = now - self._pings.popleft()
        self.stats.rtt_samples.append(rtt)
        if self._on_rtt is not None:
            self._on_rtt(rtt, self)

    async def _pinger(self, ws: ClientConnection) -> None:
        max_outstanding = max(1, int(self._stale_after_s / self._ping_interval_s) + 1)
        while True:
            await asyncio.sleep(self._ping_interval_s)
            self._pings.append(mono_ns())
            while len(self._pings) > max_outstanding:
                self._pings.popleft()
            await ws.send("PING")

    async def _watchdog(self, ws: ClientConnection) -> None:
        stale_ns = int(self._stale_after_s * NS_PER_S)
        while True:
            await asyncio.sleep(min(1.0, self._stale_after_s / 4))
            if mono_ns() - self._last_alive_mono > stale_ns:
                self.stats.stale_closes += 1
                self._control(ControlEvent.STALE, stale_after_s=self._stale_after_s)
                log.warning("ws_stale_closing", conn=self.conn_id)
                await ws.close()
                return

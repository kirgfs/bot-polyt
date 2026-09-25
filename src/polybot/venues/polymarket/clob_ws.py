"""CLOB market channel: a pool of connections, each with at most N assets (docs/api_notes.md §10).

Every raw frame goes to the sink; events also feed the BookTracker. On a desync (or a
missing snapshot after subscribe) the asset is resubscribed on its connection, which
makes the server send a fresh `book` snapshot.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass, field

from polybot.core.config import MarketWsConfig
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns
from polybot.core.ws import ControlEvent, Heartbeat, WsConnection
from polybot.data.records import Kind, Record, RecordWriter, Source
from polybot.venues.polymarket.orderbook import BookTracker, Desync, iter_events

log = get_logger(__name__)

# Cap on resync attempts per asset without a snapshot in between; after that the
# asset is reported as dead until the next discovery-driven subscription change.
MAX_RESYNC_ATTEMPTS = 5


def subscribe_initial(assets: Iterable[str], custom: bool) -> str:
    # [SDK] market_protocol.build_initial_frame
    return json.dumps(
        {"type": "market", "assets_ids": sorted(assets), "custom_feature_enabled": custom}
    )


def subscribe_update(assets: Iterable[str], custom: bool) -> str:
    return json.dumps(
        {"operation": "subscribe", "assets_ids": sorted(assets), "custom_feature_enabled": custom}
    )


def unsubscribe_update(assets: Iterable[str]) -> str:
    return json.dumps({"operation": "unsubscribe", "assets_ids": sorted(assets)})


@dataclass
class AssetState:
    conn_index: int
    awaiting_since_mono: int | None = None  # set on (re)subscribe, cleared by a snapshot
    last_resync_mono: int = 0
    resync_attempts: int = 0
    dead: bool = False


@dataclass
class PoolStats:
    resyncs: int = 0
    snapshot_timeouts: int = 0
    dead_assets: int = 0
    frames_unparsed: int = 0
    subscribe_ops: int = 0
    unsubscribe_ops: int = 0
    desync_examples: list[str] = field(default_factory=list)


class _MarketConn:
    def __init__(self, index: int, pool: MarketPool) -> None:
        self.index = index
        self.assets: set[str] = set()
        self._pool = pool
        cfg = pool.cfg
        self.ws = WsConnection(
            name=f"clob-market-{index}",
            url=pool.url,
            heartbeat=Heartbeat.CLIENT_PING,
            on_frame=pool.on_frame,
            on_open=self._on_open,
            on_control=pool.on_control,
            on_rtt=pool.on_rtt,
            ping_interval_s=cfg.ping_interval_s,
            stale_after_s=cfg.stale_after_s,
            open_timeout_s=cfg.open_timeout_s,
            max_frame_bytes=cfg.max_frame_bytes,
        )
        self.task: asyncio.Task[None] | None = None

    async def _on_open(self, conn: WsConnection) -> None:
        # Each (re)connect starts a new book epoch for every asset on this connection.
        for asset in self.assets:
            self._pool.mark_awaiting(asset)
        if self.assets:
            await conn.send(subscribe_initial(self.assets, self._pool.cfg.custom_feature_enabled))
        self._pool.control_record(conn, "subscribe_initial", n_assets=len(self.assets))


class MarketPool:
    def __init__(
        self, cfg: MarketWsConfig, url: str, sink: RecordWriter, tracker: BookTracker
    ) -> None:
        self.cfg = cfg
        self.url = url
        self._sink = sink
        self.tracker = tracker
        self._conns: list[_MarketConn] = []
        self.assets: dict[str, AssetState] = {}
        self.stats = PoolStats()
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ subscription management

    async def set_assets(self, desired: set[str]) -> None:
        async with self._lock:
            removed = set(self.assets) - desired
            added = desired - set(self.assets)
            by_conn: dict[int, set[str]] = {}
            for asset in removed:
                by_conn.setdefault(self.assets.pop(asset).conn_index, set()).add(asset)
                self.tracker.forget(asset)
            for index, assets in by_conn.items():
                conn = self._conns[index]
                conn.assets -= assets
                if not conn.assets and conn.task is not None:
                    # Nothing left on this connection: close it; _assign may reuse the slot.
                    conn.task.cancel()
                    await asyncio.gather(conn.task, return_exceptions=True)
                    conn.task = None
                elif conn.ws.is_open and await conn.ws.send(unsubscribe_update(assets)):
                    self.stats.unsubscribe_ops += 1
            for index, assets in self._assign(added).items():
                conn = self._conns[index]
                conn.assets |= assets
                for asset in assets:
                    self.assets[asset] = AssetState(conn_index=index)
                    self.mark_awaiting(asset)
                if conn.task is None:
                    conn.task = asyncio.create_task(conn.ws.run(), name=conn.ws.name)
                elif conn.ws.is_open and await conn.ws.send(
                    subscribe_update(assets, self.cfg.custom_feature_enabled)
                ):
                    self.stats.subscribe_ops += 1
            if added or removed:
                log.info(
                    "market_pool_updated",
                    added=len(added),
                    removed=len(removed),
                    assets=len(self.assets),
                    conns=sum(1 for c in self._conns if c.task is not None),
                )

    def _assign(self, added: set[str]) -> dict[int, set[str]]:
        """Fill existing connections first, then open new ones (lazy pool growth)."""
        plan: dict[int, set[str]] = {}
        pending = sorted(added)
        for conn in self._conns:
            room = self.cfg.max_assets_per_conn - len(conn.assets)
            if room > 0 and pending:
                take, pending = pending[:room], pending[room:]
                plan[conn.index] = set(take)
        while pending:
            conn = _MarketConn(len(self._conns), self)
            self._conns.append(conn)
            take, pending = (
                pending[: self.cfg.max_assets_per_conn],
                pending[self.cfg.max_assets_per_conn :],
            )
            plan[conn.index] = set(take)
        return plan

    def mark_awaiting(self, asset: str) -> None:
        state = self.assets.get(asset)
        if state is not None:
            state.awaiting_since_mono = mono_ns()
        self.tracker.expect_snapshot(asset)

    async def resync(self, asset: str, reason: str) -> None:
        state = self.assets.get(asset)
        if state is None or state.dead:
            return
        now = mono_ns()
        if now - state.last_resync_mono < self.cfg.resync_min_interval_s * NS_PER_S:
            return
        if state.resync_attempts >= MAX_RESYNC_ATTEMPTS:
            state.dead = True
            self.stats.dead_assets += 1
            log.warning("asset_resync_exhausted", asset=asset[:12], reason=reason)
            return
        state.last_resync_mono = now
        state.resync_attempts += 1
        conn = self._conns[state.conn_index]
        if not conn.ws.is_open:
            return  # reconnect will resubscribe everything
        self.mark_awaiting(asset)
        await conn.ws.send(unsubscribe_update([asset]))
        await conn.ws.send(subscribe_update([asset], self.cfg.custom_feature_enabled))
        self.stats.resyncs += 1
        self.control_record(conn.ws, "resync", asset_id=asset, reason=reason)

    async def run_snapshot_watch(self) -> None:
        """Resubscribe assets that did not get a `book` within snapshot_timeout_s."""
        timeout_ns = int(self.cfg.snapshot_timeout_s * NS_PER_S)
        while True:
            await asyncio.sleep(max(1.0, self.cfg.snapshot_timeout_s / 2))
            now = mono_ns()
            late = [
                asset
                for asset, state in self.assets.items()
                if state.awaiting_since_mono is not None
                and not state.dead
                and now - state.awaiting_since_mono > timeout_ns
                and self._conns[state.conn_index].ws.is_open
            ]
            for asset in late:
                self.stats.snapshot_timeouts += 1
                await self.resync(asset, "snapshot_timeout")

    async def close(self) -> None:
        tasks = [c.task for c in self._conns if c.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for conn in self._conns:
            await conn.ws.close()

    # ------------------------------------------------------------------- frame handling

    def on_frame(self, text: str, ts_recv_ns: int, conn: WsConnection) -> None:
        record = Record(
            ts_recv_ns=ts_recv_ns,
            source=Source.CLOB_MARKET_WS,
            kind=Kind.FRAME,
            payload=text,
            conn_id=conn.conn_id,
        )
        try:
            events = list(iter_events(text))
        except (json.JSONDecodeError, RecursionError):
            self.stats.frames_unparsed += 1
            record.event_type = "unparsed"
            self._sink.write(record)
            return
        if events:
            first = events[0]
            record.event_type = str(first.get("event_type") or first.get("type") or "") or None
            record.n_events = len(events)
            record.market = _opt(first.get("market"))
            record.asset_id = _opt(first.get("asset_id"))
            record.server_ts_ms = _opt_int(first.get("timestamp"))
        self._sink.write(record)
        for event in events:
            if event.get("event_type") == "book":
                state = self.assets.get(str(event.get("asset_id") or ""))
                if state is not None:
                    state.awaiting_since_mono = None
                    state.resync_attempts = 0
                    state.dead = False
            for desync in self.tracker.on_event(event, ts_recv_ns):
                self._on_desync(desync)

    def _on_desync(self, desync: Desync) -> None:
        if len(self.stats.desync_examples) < 20:
            self.stats.desync_examples.append(f"{desync.reason}:{desync.detail}")
        self._sink.write(
            Record(
                ts_recv_ns=now_ns(),
                source=Source.CLOB_MARKET_WS,
                kind=Kind.CONTROL,
                event_type="desync",
                asset_id=desync.asset_id,
                payload=json.dumps({"reason": desync.reason.value, "detail": desync.detail}),
            )
        )
        task = asyncio.get_running_loop().create_task(
            self.resync(desync.asset_id, desync.reason.value)
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def on_control(self, event: ControlEvent, info: dict[str, object], conn: WsConnection) -> None:
        self.control_record(conn, event.value, **info)

    def on_rtt(self, rtt_ns: int, conn: WsConnection) -> None:
        self._sink.write(
            Record(
                ts_recv_ns=now_ns(),
                source=Source.CLOB_MARKET_WS,
                kind=Kind.PROBE,
                event_type="ping_rtt",
                conn_id=conn.conn_id,
                latency_ns=rtt_ns,
                payload="",
            )
        )

    def control_record(self, conn: WsConnection, event: str, **info: object) -> None:
        self._sink.write(
            Record(
                ts_recv_ns=now_ns(),
                source=Source.CLOB_MARKET_WS,
                kind=Kind.CONTROL,
                event_type=event,
                conn_id=conn.conn_id,
                asset_id=_opt(info.pop("asset_id", None)),
                payload=json.dumps(info, default=str),
            )
        )

    def health(self) -> dict[str, object]:
        conns = [c for c in self._conns if c.task is not None]
        return {
            "assets": len(self.assets),
            "conns": len(conns),
            "conns_open": sum(1 for c in conns if c.ws.is_open),
            "awaiting_snapshot": sum(1 for s in self.assets.values() if s.awaiting_since_mono),
            "frames": sum(c.ws.stats.frames for c in conns),
            "reconnects": sum(max(0, c.ws.stats.connects - 1) for c in conns),
            "stale_closes": sum(c.ws.stats.stale_closes for c in conns),
            "resyncs": self.stats.resyncs,
            "snapshot_timeouts": self.stats.snapshot_timeouts,
            "dead_assets": self.stats.dead_assets,
            "frames_unparsed": self.stats.frames_unparsed,
            "tracker": {
                "snapshots": self.tracker.stats.snapshots,
                "changes": self.tracker.stats.changes,
                "top_checks": self.tracker.stats.top_checks,
                "desyncs": dict(self.tracker.stats.desyncs),
                "deltas_before_snapshot": self.tracker.stats.deltas_before_snapshot,
                "trades": self.tracker.stats.trades,
                "book_levels": sum(len(b.bids) + len(b.asks) for b in self.tracker.books.values()),
            },
        }


def _opt(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _opt_int(value: object) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None

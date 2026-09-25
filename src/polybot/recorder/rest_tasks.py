"""Periodic REST work of the recorder: book validation, market metadata, rewards, RTT probe."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from polybot.core.config import ClobMetaConfig, ProbeConfig, RestValidationConfig
from polybot.core.http import TimedResponse
from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns
from polybot.data.records import Kind, Record, RecordWriter, Source
from polybot.venues.polymarket.clob_rest import ClobPublic, cf_colo
from polybot.venues.polymarket.clob_ws import MarketPool
from polybot.venues.polymarket.orderbook import DesyncReason, compare_with_rest

log = get_logger(__name__)

END_CURSOR = "LTE="  # docs/api_notes.md §2 ([SDK] actions/_cursor.py)
# Cloudflare IP limits are not published (docs/api_notes.md §7): back off on HTTP 429.
RATE_LIMIT_PAUSE_S = 60.0


def rest_record(
    source: Source, endpoint: str, response: TimedResponse, *, key: str | None = None
) -> Record:
    return Record(
        ts_recv_ns=response.ts_recv_ns,
        source=source,
        kind=Kind.REST,
        payload=response.text,
        endpoint=endpoint,
        status=response.status,
        latency_ns=response.latency_ns,
        key=key,
    )


@dataclass
class ValidatorStats:
    requests: int = 0
    errors: int = 0
    compared: int = 0
    hash_equal: int = 0
    levels_mismatch: int = 0


class RestValidator:
    """Round-robin POST /books over subscribed tokens; compare with our books when hashes match."""

    def __init__(
        self, clob: ClobPublic, pool: MarketPool, cfg: RestValidationConfig, sink: RecordWriter
    ) -> None:
        self._clob = clob
        self._pool = pool
        self._cfg = cfg
        self._sink = sink
        self._cursor = 0
        self.stats = ValidatorStats()

    def _next_batch(self) -> list[str]:
        ready = sorted(
            asset
            for asset in self._pool.assets
            if (book := self._pool.tracker.books.get(asset)) is not None and book.ready
        )
        if not ready:
            return []
        if self._cursor >= len(ready):
            self._cursor = 0
        batch = ready[self._cursor : self._cursor + self._cfg.batch_size]
        self._cursor += len(batch)
        return batch

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.interval_s)
            batch = self._next_batch()
            if batch:
                await self.check(batch)

    async def check(self, batch: list[str]) -> None:
        self.stats.requests += 1
        try:
            response = await self._clob.books(batch)
        except httpx.HTTPError as exc:
            self.stats.errors += 1
            log.warning("rest_books_failed", error=type(exc).__name__)
            return
        record = rest_record(Source.CLOB_REST_BOOKS, "/books", response)
        record.n_events = len(batch)
        self._sink.write(record)
        if response.status == 429:
            self.stats.errors += 1
            log.warning("rest_books_rate_limited", pause_s=RATE_LIMIT_PAUSE_S)
            await asyncio.sleep(RATE_LIMIT_PAUSE_S)
            return
        if not response.ok:
            self.stats.errors += 1
            return
        try:
            data = response.json()
        except json.JSONDecodeError:
            self.stats.errors += 1
            return
        for obj in data if isinstance(data, list) else []:
            if not isinstance(obj, dict):
                continue
            asset = str(obj.get("asset_id") or "")
            book = self._pool.tracker.books.get(asset)
            if book is None:
                continue
            comparison = compare_with_rest(book, obj)
            if comparison is None:
                continue
            self.stats.compared += 1
            if not comparison.hash_equal:
                continue
            self.stats.hash_equal += 1
            if comparison.levels_equal is False:
                self.stats.levels_mismatch += 1
                self._pool.tracker.stats.count_desync(DesyncReason.REST_MISMATCH)
                await self._pool.resync(asset, DesyncReason.REST_MISMATCH.value)


class ClobMetaCollector:
    """/clob-markets/{cid} per recorded market (tick, fees) and /rewards/markets/current."""

    def __init__(self, clob: ClobPublic, cfg: ClobMetaConfig, sink: RecordWriter) -> None:
        self._clob = clob
        self._cfg = cfg
        self._sink = sink
        self._fetched_mono: dict[str, int] = {}
        self.condition_ids: set[str] = set()
        self._last_rewards_mono = 0
        self.errors = 0

    async def run(self) -> None:
        while True:
            await self._refresh_markets()
            if mono_ns() - self._last_rewards_mono >= self._cfg.rewards_interval_s * NS_PER_S:
                await self._fetch_rewards()
                self._last_rewards_mono = mono_ns()
            await asyncio.sleep(30)

    async def _refresh_markets(self) -> None:
        refresh_ns = int(self._cfg.clob_markets_refresh_s * NS_PER_S)
        now = mono_ns()
        due = [
            cid
            for cid in sorted(self.condition_ids)
            if now - self._fetched_mono.get(cid, -refresh_ns) >= refresh_ns
        ]
        for cid in due[:200]:
            try:
                response = await self._clob.clob_market(cid)
            except httpx.HTTPError:
                self.errors += 1
                continue
            self._sink.write(rest_record(Source.CLOB_MARKETS, "/clob-markets", response, key=cid))
            if response.status == 429:
                self.errors += 1
                log.warning("clob_markets_rate_limited", pause_s=RATE_LIMIT_PAUSE_S)
                await asyncio.sleep(RATE_LIMIT_PAUSE_S)
                return
            if response.ok:
                self._fetched_mono[cid] = mono_ns()
            await asyncio.sleep(0.2)

    async def _fetch_rewards(self) -> None:
        cursor: str | None = None
        for _ in range(self._cfg.rewards_max_pages):
            try:
                response = await self._clob.rewards_current(cursor)
            except httpx.HTTPError:
                self.errors += 1
                return
            self._sink.write(rest_record(Source.CLOB_REWARDS, "/rewards/markets/current", response))
            if not response.ok:
                self.errors += 1
                return
            try:
                data = response.json()
            except json.JSONDecodeError:
                self.errors += 1
                return
            next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == END_CURSOR:
                return
            cursor = next_cursor
            await asyncio.sleep(0.2)


class RestProbe:
    """GET /time on CLOB every N seconds: continuous REST round-trip samples for latency.md."""

    def __init__(self, clob: ClobPublic, cfg: ProbeConfig, sink: RecordWriter) -> None:
        self._clob = clob
        self._cfg = cfg
        self._sink = sink
        self.last_latency_ms: float | None = None

    async def run(self) -> None:
        while True:
            await self.probe_once()
            await asyncio.sleep(self._cfg.rest_interval_s)

    async def probe_once(self) -> None:
        try:
            response = await self._clob.server_time()
        except httpx.HTTPError as exc:
            self._sink.write(
                Record(
                    ts_recv_ns=now_ns(),
                    source=Source.PROBE_REST,
                    kind=Kind.PROBE,
                    event_type="clob_time_error",
                    endpoint="/time",
                    payload=json.dumps({"error": type(exc).__name__}),
                )
            )
            return
        self.last_latency_ms = response.latency_ns / 1e6
        self._sink.write(
            Record(
                ts_recv_ns=response.ts_recv_ns,
                source=Source.PROBE_REST,
                kind=Kind.PROBE,
                event_type="clob_time",
                endpoint="/time",
                status=response.status,
                latency_ns=response.latency_ns,
                payload=json.dumps(
                    {"body": response.text[:200], "colo": cf_colo(response.headers)}
                ),
            )
        )

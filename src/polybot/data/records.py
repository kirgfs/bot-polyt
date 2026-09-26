"""One row format for every raw input: WS frames, REST responses, control events, probes.

Rows are written to Parquet partitioned by `date=YYYY-MM-DD/source=<name>` (UTC date of
`ts_recv_ns`). `payload` holds the raw text exactly as received, so parsing can change
later without re-recording.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import pyarrow as pa


class Kind(StrEnum):
    FRAME = "frame"  # raw WS frame
    REST = "rest"  # raw REST response body
    CONTROL = "control"  # connect/disconnect/subscribe/resync/gap markers (payload is JSON)
    PROBE = "probe"  # latency measurement (latency_ns set)


class Source(StrEnum):
    CLOB_MARKET_WS = "clob_market_ws"
    SPORTS_WS = "sports_ws"
    GAMMA_EVENTS = "gamma_events"
    GAMMA_META = "gamma_meta"  # /sports, /tags, /sports/market-types
    CLOB_REST_BOOKS = "clob_rest_books"
    CLOB_MARKETS = "clob_markets"
    CLOB_REWARDS = "clob_rewards"
    GEOBLOCK = "geoblock"
    PROBE_REST = "probe_rest"
    ODDSPAPI_REST = "oddspapi_rest"
    ODDSPAPI_WS = "oddspapi_ws"
    RECORDER = "recorder"  # recorder lifecycle and health snapshots
    PAPER = "paper"  # mini-bot paper orders, fills, settlements, P&L (never real orders)


@dataclass(slots=True)
class Record:
    ts_recv_ns: int
    source: str
    kind: str
    payload: str
    conn_id: str | None = None
    event_type: str | None = None
    # Primary key of the payload within its source: Gamma event id, sports gameId,
    # OddsPapi fixtureId, condition id for /clob-markets. Lets analysis skip JSON parsing.
    key: str | None = None
    market: str | None = None
    asset_id: str | None = None
    server_ts_ms: int | None = None
    n_events: int | None = None
    endpoint: str | None = None
    status: int | None = None
    latency_ns: int | None = None
    seq: int = 0  # assigned by the sink: monotonic per source within one process run
    run_id: str = ""  # assigned by the sink: distinguishes process restarts


class RecordWriter(Protocol):
    """Anything that accepts raw records; ParquetSink in production, a list in tests."""

    def write(self, record: Record) -> None: ...


async def drain(writer: RecordWriter | None) -> None:
    """Backpressure point for producers that can wait: lets a ParquetSink flush if due."""
    method = getattr(writer, "drain", None)
    if method is not None:
        await method()


SCHEMA = pa.schema(
    [
        pa.field("ts_recv_ns", pa.int64(), nullable=False),
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("conn_id", pa.string()),
        pa.field("event_type", pa.string()),
        pa.field("key", pa.string()),
        pa.field("market", pa.string()),
        pa.field("asset_id", pa.string()),
        pa.field("server_ts_ms", pa.int64()),
        pa.field("n_events", pa.int32()),
        pa.field("endpoint", pa.string()),
        pa.field("status", pa.int32()),
        pa.field("latency_ns", pa.int64()),
        pa.field("payload", pa.string(), nullable=False),
    ]
)


def records_to_table(rows: list[Record]) -> pa.Table:
    return pa.table(
        {
            "ts_recv_ns": [r.ts_recv_ns for r in rows],
            "run_id": [r.run_id for r in rows],
            "seq": [r.seq for r in rows],
            "kind": [r.kind for r in rows],
            "conn_id": [r.conn_id for r in rows],
            "event_type": [r.event_type for r in rows],
            "key": [r.key for r in rows],
            "market": [r.market for r in rows],
            "asset_id": [r.asset_id for r in rows],
            "server_ts_ms": [r.server_ts_ms for r in rows],
            "n_events": [r.n_events for r in rows],
            "endpoint": [r.endpoint for r in rows],
            "status": [r.status for r in rows],
            "latency_ns": [r.latency_ns for r in rows],
            "payload": [r.payload for r in rows],
        },
        schema=SCHEMA,
    )

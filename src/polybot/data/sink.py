"""Buffered Parquet sink for raw records.

Each flush writes one new file per (date, source): write to a hidden temp file, then
`os.replace`. A crash loses at most one flush interval and never leaves a truncated
Parquet file in the dataset. `compact_day` later merges small files hour by hour.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, now_ns, ns_to_datetime
from polybot.data.records import SCHEMA, Record, records_to_table

log = get_logger(__name__)


@dataclass
class SinkStats:
    rows_written: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    files_written: int = 0
    write_errors: int = 0
    dropped_rows: int = 0
    last_flush_ns: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "rows_written": dict(self.rows_written),
            "files_written": self.files_written,
            "write_errors": self.write_errors,
            "dropped_rows": self.dropped_rows,
            "last_flush_ns": self.last_flush_ns,
        }


class ParquetSink:
    def __init__(
        self,
        root: Path,
        *,
        flush_interval_s: float = 15.0,
        max_buffer_rows: int = 2_000_000,
        compression_level: int = 6,
    ) -> None:
        self._root = root
        self._flush_interval_s = flush_interval_s
        self._max_buffer_rows = max_buffer_rows
        self._compression_level = compression_level
        self._buffers: dict[str, list[Record]] = defaultdict(list)
        self._seq: dict[str, int] = defaultdict(int)
        self._buffered = 0
        self._flush_lock = asyncio.Lock()
        self.run_id = uuid.uuid4().hex[:12]
        self.stats = SinkStats()

    @property
    def root(self) -> Path:
        return self._root

    def write(self, record: Record) -> None:
        """Append a record. Never blocks and never raises: the event loop owns this call."""
        record.source = str(record.source)  # plain str keys for stats and paths, not enums
        record.kind = str(record.kind)
        self._seq[record.source] += 1
        record.seq = self._seq[record.source]
        record.run_id = self.run_id
        self._buffers[record.source].append(record)
        self._buffered += 1
        if self._buffered > self._max_buffer_rows:
            self._drop_oldest()

    def _drop_oldest(self) -> None:
        # Disk is failing and the buffer is full: shed the largest source first.
        source = max(self._buffers, key=lambda s: len(self._buffers[s]))
        excess = self._buffered - self._max_buffer_rows
        del self._buffers[source][:excess]
        self._buffered -= excess
        self.stats.dropped_rows += excess
        log.error("sink_buffer_overflow_dropped_rows", source=source, dropped=excess)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            await self.flush()

    async def flush(self) -> None:
        async with self._flush_lock:
            pending = {src: rows for src, rows in self._buffers.items() if rows}
            if not pending:
                return
            self._buffers = defaultdict(list)
            self._buffered = 0
            for source, rows in pending.items():
                try:
                    await asyncio.to_thread(self._write_rows, source, rows)
                except Exception:
                    self.stats.write_errors += 1
                    log.exception("sink_write_failed", source=source, rows=len(rows))
                    # Keep the rows for the next attempt, ahead of anything newer.
                    self._buffers[source][:0] = rows
                    self._buffered += len(rows)
                    if self._buffered > self._max_buffer_rows:
                        self._drop_oldest()
                else:
                    self.stats.rows_written[source] += len(rows)
            self.stats.last_flush_ns = now_ns()

    def _write_rows(self, source: str, rows: list[Record]) -> None:
        by_date: dict[str, list[Record]] = defaultdict(list)
        for row in rows:
            by_date[ns_to_datetime(row.ts_recv_ns).strftime("%Y-%m-%d")].append(row)
        for day, day_rows in by_date.items():
            directory = self._root / f"date={day}" / f"source={source}"
            directory.mkdir(parents=True, exist_ok=True)
            first = day_rows[0]
            stamp = ns_to_datetime(first.ts_recv_ns).strftime("%H%M%S")
            name = f"part-{stamp}-{first.run_id}-{first.seq:012d}.parquet"
            final = directory / name
            tmp = directory / f".{name}.tmp"
            pq.write_table(
                records_to_table(day_rows),
                tmp,
                compression="zstd",
                compression_level=self._compression_level,
            )
            os.replace(tmp, final)
            self.stats.files_written += 1

    async def close(self) -> None:
        await self.flush()


def compact_day(root: Path, day: str) -> int:
    """Merge small part files of one finished UTC day into one file per source and hour.

    Returns the number of files removed. Must not run on the current day: the live
    writer keeps adding parts there.
    """
    today = ns_to_datetime(now_ns()).strftime("%Y-%m-%d")
    if day >= today:
        raise ValueError(f"refusing to compact {day}: only finished days (before {today})")
    removed = 0
    day_dir = root / f"date={day}"
    for source_dir in sorted(p for p in day_dir.glob("source=*") if p.is_dir()):
        parts = sorted(source_dir.glob("part-*.parquet"))
        if len(parts) < 2:
            continue
        table = pq.read_table(parts, schema=SCHEMA).sort_by(
            [("ts_recv_ns", "ascending"), ("run_id", "ascending"), ("seq", "ascending")]
        )
        hours = [ts // (3600 * NS_PER_S) for ts in table.column("ts_recv_ns").to_pylist()]
        start = 0
        while start < len(hours):
            end = start
            while end < len(hours) and hours[end] == hours[start]:
                end += 1
            chunk = table.slice(start, end - start)
            stamp = ns_to_datetime(hours[start] * 3600 * NS_PER_S).strftime("%H")
            name = f"compacted-{stamp}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = source_dir / f".{name}.tmp"
            pq.write_table(chunk, tmp, compression="zstd", compression_level=9)
            os.replace(tmp, source_dir / name)
            start = end
        for part in parts:
            part.unlink()
            removed += 1
    return removed

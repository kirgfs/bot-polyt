"""Buffered Parquet sink for raw records, with a hard memory bound.

Each flush writes one new file per (date, source): write to a hidden temp file, then
`os.replace`. A crash loses at most one flush interval and never leaves a truncated
Parquet file in the dataset. `compact_day` later merges small files hour by hour.

Memory: the buffer is flushed on whichever comes first — `flush_interval_s`, `flush_rows`
buffered rows or `flush_mb` buffered payload. Producers that can wait (discovery, REST
tasks) call `drain()` after a burst, so they never outrun the disk. If writes keep failing,
the buffer is capped at `max_buffer_mb`: the oldest rows of the largest source are dropped
and counted, instead of growing until the OOM killer ends the process.
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
from polybot.core.timeutil import now_ns, ns_to_datetime
from polybot.data.records import SCHEMA, Record, records_to_table

log = get_logger(__name__)

MB = 1024 * 1024
# Python object overhead per buffered record on top of its payload (Record, small strings).
ROW_OVERHEAD_BYTES = 320


def record_bytes(record: Record) -> int:
    return len(record.payload) + ROW_OVERHEAD_BYTES


@dataclass
class SinkStats:
    rows_written: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    files_written: int = 0
    write_errors: int = 0
    dropped_rows: int = 0
    last_flush_ns: int = 0
    buffered_rows: int = 0
    buffered_mb: float = 0.0
    peak_buffered_mb: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "rows_written": dict(self.rows_written),
            "files_written": self.files_written,
            "write_errors": self.write_errors,
            "dropped_rows": self.dropped_rows,
            "last_flush_ns": self.last_flush_ns,
            "buffered_rows": self.buffered_rows,
            "buffered_mb": round(self.buffered_mb, 1),
            "peak_buffered_mb": round(self.peak_buffered_mb, 1),
        }


class ParquetSink:
    def __init__(
        self,
        root: Path,
        *,
        flush_interval_s: float = 10.0,
        flush_rows: int = 20_000,
        flush_mb: float = 8.0,
        max_buffer_mb: float = 64.0,
        max_buffer_rows: int = 500_000,
        compression_level: int = 6,
    ) -> None:
        self._root = root
        self._flush_interval_s = flush_interval_s
        self._flush_rows = flush_rows
        self._flush_bytes = int(flush_mb * MB)
        self._max_bytes = int(max_buffer_mb * MB)
        self._max_rows = max_buffer_rows
        self._compression_level = compression_level
        self._buffers: dict[str, list[Record]] = defaultdict(list)
        self._source_bytes: dict[str, int] = defaultdict(int)
        self._seq: dict[str, int] = defaultdict(int)
        self._rows = 0
        self._bytes = 0
        self._flush_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self.run_id = uuid.uuid4().hex[:12]
        self.stats = SinkStats()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def buffered_bytes(self) -> int:
        return self._bytes

    def write(self, record: Record) -> None:
        """Append a record. Never blocks and never raises: the event loop owns this call."""
        record.source = str(record.source)  # plain str keys for stats and paths, not enums
        record.kind = str(record.kind)
        self._seq[record.source] += 1
        record.seq = self._seq[record.source]
        record.run_id = self.run_id
        self._buffers[record.source].append(record)
        self._account(record.source, 1, record_bytes(record))
        if self._rows >= self._flush_rows or self._bytes >= self._flush_bytes:
            self._wakeup.set()
        if self._rows > self._max_rows or self._bytes > self._max_bytes:
            self._drop_oldest()

    def _account(self, source: str, rows: int, size: int) -> None:
        self._source_bytes[source] += size
        self._rows += rows
        self._bytes += size
        self._publish_stats()

    def _publish_stats(self) -> None:
        self.stats.buffered_rows = self._rows
        self.stats.buffered_mb = self._bytes / MB
        self.stats.peak_buffered_mb = max(self.stats.peak_buffered_mb, self.stats.buffered_mb)

    def _drop_oldest(self) -> None:
        # Disk is failing or too slow and the buffer is full: shed the largest source first.
        dropped: dict[str, int] = defaultdict(int)
        while self._rows > self._max_rows or self._bytes > self._max_bytes:
            nonempty = [s for s, rows in self._buffers.items() if rows]
            if not nonempty:
                break
            source = max(nonempty, key=lambda s: self._source_bytes[s])
            rows = self._buffers[source]
            cut = max(1, len(rows) // 10)
            freed = sum(record_bytes(r) for r in rows[:cut])
            del rows[:cut]
            self._account(source, -cut, -freed)
            dropped[source] += cut
        total = sum(dropped.values())
        self.stats.dropped_rows += total
        log.error("sink_buffer_overflow_dropped_rows", dropped=dict(dropped), total=total)

    async def run(self) -> None:
        while True:
            try:
                async with asyncio.timeout(self._flush_interval_s):
                    await self._wakeup.wait()
            except TimeoutError:
                pass
            self._wakeup.clear()
            await self.flush()

    async def drain(self) -> None:
        """Backpressure for producers that can wait: flush now if a flush is due."""
        if self._rows >= self._flush_rows or self._bytes >= self._flush_bytes:
            await self.flush()

    async def flush(self) -> None:
        async with self._flush_lock:
            pending = {src: rows for src, rows in self._buffers.items() if rows}
            if not pending:
                return
            self._buffers = defaultdict(list)
            self._source_bytes = defaultdict(int)
            self._rows = self._bytes = 0
            self._publish_stats()
            for source, rows in pending.items():
                # Bounded Arrow tables: the conversion copies the payloads once more.
                for start, end in self._chunks(rows):
                    chunk = rows[start:end]
                    try:
                        await asyncio.to_thread(self._write_rows, source, chunk)
                    except Exception:
                        self.stats.write_errors += 1
                        log.exception("sink_write_failed", source=source, rows=len(chunk))
                        # Keep this and later chunks for the next attempt, ahead of newer rows.
                        failed = rows[start:]
                        self._buffers[source][:0] = failed
                        self._account(source, len(failed), sum(record_bytes(r) for r in failed))
                        if self._rows > self._max_rows or self._bytes > self._max_bytes:
                            self._drop_oldest()
                        break
                    self.stats.rows_written[source] += len(chunk)
            self.stats.last_flush_ns = now_ns()

    def _chunks(self, rows: list[Record]) -> list[tuple[int, int]]:
        """Split into [start, end) spans of at most flush_rows rows and ~flush_mb payload."""
        spans: list[tuple[int, int]] = []
        start = size = 0
        for i, row in enumerate(rows):
            size += record_bytes(row)
            if i + 1 - start >= self._flush_rows or size >= self._flush_bytes:
                spans.append((start, i + 1))
                start, size = i + 1, 0
        if start < len(rows):
            spans.append((start, len(rows)))
        return spans

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

    Streams part by part (memory stays at one part file, not one day of data). Parts go
    into the hour of their first row, in file-name order (time, then run and sequence);
    rows keep their `ts_recv_ns`, so analysis never depends on the file boundaries.
    Returns the number of part files removed. Refuses the current day: the live writer
    keeps adding parts there.
    """
    today = ns_to_datetime(now_ns()).strftime("%Y-%m-%d")
    if day >= today:
        raise ValueError(f"refusing to compact {day}: only finished days (before {today})")
    removed = 0
    day_dir = root / f"date={day}"
    for source_dir in sorted(p for p in day_dir.glob("source=*") if p.is_dir()):
        all_parts = sorted(source_dir.glob("part-*.parquet"))
        if len(all_parts) < 2:
            continue
        by_hour: dict[str, list[Path]] = defaultdict(list)
        for part in all_parts:
            by_hour[part.name.split("-")[1][:2]].append(part)
        for hour, parts in sorted(by_hour.items()):
            name = f"compacted-{hour}-{uuid.uuid4().hex[:8]}.parquet"
            tmp = source_dir / f".{name}.tmp"
            with pq.ParquetWriter(tmp, SCHEMA, compression="zstd", compression_level=9) as writer:
                for part in parts:
                    writer.write_table(pq.read_table(part, schema=SCHEMA))
            os.replace(tmp, source_dir / name)
            for part in parts:
                part.unlink()
                removed += 1
    return removed

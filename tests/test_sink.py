from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from polybot.core.timeutil import NS_PER_S, parse_ts_ns
from polybot.data.records import Kind, Record
from polybot.data.sink import ParquetSink, compact_day
from polybot.data.store import connect, query, scan

DAY1 = parse_ts_ns("2026-09-20T23:59:59Z") or 0
DAY2 = parse_ts_ns("2026-09-21T00:00:01Z") or 0


def rec(ts: int, source: str = "clob_market_ws", payload: str = "{}") -> Record:
    return Record(ts_recv_ns=ts, source=source, kind=Kind.FRAME, payload=payload)


async def test_flush_partitions_by_utc_date_and_source(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path)
    sink.write(rec(DAY1, payload='{"a":1}'))
    sink.write(rec(DAY2, payload='{"a":2}'))
    sink.write(rec(DAY2, source="sports_ws"))
    await sink.close()
    files = sorted(p.relative_to(tmp_path).parts[:2] for p in tmp_path.rglob("*.parquet"))
    assert files == [
        ("date=2026-09-20", "source=clob_market_ws"),
        ("date=2026-09-21", "source=clob_market_ws"),
        ("date=2026-09-21", "source=sports_ws"),
    ]
    assert not list(tmp_path.rglob("*.tmp"))
    con = connect()
    table = scan(tmp_path, "clob_market_ws")
    assert table is not None
    rows = query(con, f"SELECT seq, payload, run_id, date FROM {table} ORDER BY seq")
    assert [(r[0], r[1]) for r in rows] == [(1, '{"a":1}'), (2, '{"a":2}')]
    assert rows[0][2] == sink.run_id


async def test_write_failure_keeps_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = ParquetSink(tmp_path)
    calls = {"n": 0}
    original = sink._write_rows

    def flaky(source: str, rows: list[Record]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        original(source, rows)

    monkeypatch.setattr(sink, "_write_rows", flaky)
    sink.write(rec(DAY1))
    await sink.flush()
    assert sink.stats.write_errors == 1
    assert not list(tmp_path.rglob("*.parquet"))
    await sink.flush()
    assert sink.stats.rows_written["clob_market_ws"] == 1


def test_overflow_drops_and_counts(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, max_buffer_rows=3)
    for i in range(5):
        sink.write(rec(DAY1 + i))
    assert sink.stats.dropped_rows == 2


async def test_compact_day_merges_hours(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path)
    for i in range(3):
        sink.write(rec(DAY1 - 3600 * NS_PER_S * 2 + i))
        await sink.flush()
    sink.write(rec(DAY1))
    await sink.close()
    source_dir = tmp_path / "date=2026-09-20" / "source=clob_market_ws"
    assert len(list(source_dir.glob("part-*.parquet"))) == 4
    removed = compact_day(tmp_path, "2026-09-20")
    assert removed == 4
    compacted = sorted(source_dir.glob("compacted-*.parquet"))
    assert [p.name.split("-")[1] for p in compacted] == ["21", "23"]
    assert sum(pq.read_metadata(p).num_rows for p in compacted) == 4


def test_compact_refuses_today(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only finished days"):
        compact_day(tmp_path, "2999-01-01")


async def test_flushes_on_row_threshold_before_the_interval(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, flush_interval_s=3600, flush_rows=10)
    runner = asyncio.create_task(sink.run())
    try:
        for i in range(12):
            sink.write(rec(DAY1 + i))
        for _ in range(200):
            if sink.stats.rows_written["clob_market_ws"]:
                break
            await asyncio.sleep(0.01)
        assert sink.stats.rows_written["clob_market_ws"] == 12
        assert sink.stats.buffered_rows == 0
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


async def test_drain_flushes_on_byte_threshold(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, flush_interval_s=3600, flush_mb=0.01)  # ~10 KB
    sink.write(rec(DAY1, payload="x" * 4_000))
    await sink.drain()
    assert not sink.stats.rows_written  # below the threshold: nothing to do yet
    sink.write(rec(DAY1 + 1, payload="x" * 8_000))
    await sink.drain()
    assert sink.stats.rows_written["clob_market_ws"] == 2
    assert sink.buffered_bytes == 0


async def test_large_flush_is_split_into_bounded_files(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, flush_interval_s=3600, flush_rows=4, max_buffer_rows=100)
    for i in range(10):
        sink.write(rec(DAY1 + i))
    await sink.flush()
    rows = [pq.read_metadata(p).num_rows for p in sorted(tmp_path.rglob("*.parquet"))]
    assert sorted(rows) == [2, 4, 4]


def test_byte_cap_drops_oldest_and_bounds_memory(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, max_buffer_mb=0.05)  # ~52 KB: the disk "never" catches up
    for i in range(100):
        sink.write(rec(DAY1 + i, payload="y" * 2_000))
    assert sink.buffered_bytes <= 0.05 * 1024 * 1024
    assert sink.stats.dropped_rows > 0
    assert sink.stats.buffered_rows + sink.stats.dropped_rows == 100
    # The newest rows survive.
    kept = sink._buffers["clob_market_ws"]
    assert kept[-1].ts_recv_ns == DAY1 + 99

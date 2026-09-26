"""Read side of the raw Parquet store (DuckDB over hive-partitioned files)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import duckdb


def source_glob(root: Path, source: str) -> str | None:
    """Glob for one source, or None when nothing was recorded for it yet."""
    pattern = f"date=*/source={source}/*.parquet"
    if not any(root.glob(pattern)):
        return None
    return str(root / pattern)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def scan(root: Path, source: str, *, dates: Sequence[str] | None = None) -> str | None:
    """SQL table expression for one source (hive columns `date`, `source` included).

    `dates` (YYYY-MM-DD) limits the scan to those partitions: a daily report reads one
    day of files instead of filtering every file of the retention window.
    """
    if dates is None:
        glob = source_glob(root, source)
        globs = [glob] if glob is not None else []
    else:
        globs = [
            str(root / f"date={day}" / f"source={source}" / "*.parquet")
            for day in dates
            if any((root / f"date={day}" / f"source={source}").glob("*.parquet"))
        ]
    if not globs:
        return None
    files = _quote(globs[0]) if len(globs) == 1 else "[" + ", ".join(map(_quote, globs)) + "]"
    return f"read_parquet({files}, hive_partitioning = true, union_by_name = true)"


def connect(
    memory_limit: str = "2GB", *, temp_dir: Path | None = None, threads: int | None = None
) -> duckdb.DuckDBPyConnection:
    """DuckDB for reports. On the VPS the memory limit is small; overflow spills to `temp_dir`."""
    con = duckdb.connect()
    con.execute(f"SET memory_limit = {_quote(memory_limit)}")
    if temp_dir is not None:
        temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = {_quote(str(temp_dir))}")
    if threads is not None:
        con.execute(f"SET threads = {int(threads)}")
    # Insertion order is irrelevant for our aggregations; dropping it lowers memory.
    con.execute("SET preserve_insertion_order = false")
    return con


def query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[tuple[Any, ...]]:
    return con.execute(sql, params or []).fetchall()


def iter_query(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | None = None,
    *,
    batch: int = 500,
) -> Iterator[tuple[Any, ...]]:
    """Rows in batches: large payload columns never sit in Python memory all at once.

    Runs on its own cursor, so other queries on `con` while the caller iterates do not
    cut the result short. The cursor does not see `con`'s TEMP tables: raw tables only.
    """
    cursor = con.cursor()
    try:
        cursor.execute(sql, params or [])
        while rows := cursor.fetchmany(batch):
            yield from rows
    finally:
        cursor.close()

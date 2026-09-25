"""Read side of the raw Parquet store (DuckDB over hive-partitioned files)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


def source_glob(root: Path, source: str) -> str | None:
    """Glob for one source, or None when nothing was recorded for it yet."""
    pattern = f"date=*/source={source}/*.parquet"
    if not any(root.glob(pattern)):
        return None
    return str(root / pattern)


def scan(root: Path, source: str) -> str | None:
    """SQL table expression for one source (hive columns `date`, `source` included)."""
    glob = source_glob(root, source)
    if glob is None:
        return None
    escaped = glob.replace("'", "''")
    return f"read_parquet('{escaped}', hive_partitioning = true, union_by_name = true)"


def connect(memory_limit: str = "2GB") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    return con


def query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> list[tuple[Any, ...]]:
    return con.execute(sql, params or []).fetchall()

"""SQLite cache (WAL). Raw API payloads are stored as JSON; time series in their own tables for incremental fetches."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, fetched_at INTEGER, data TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS leaderboard (
    address TEXT PRIMARY KEY, fetched_at INTEGER, account_value REAL, display_name TEXT, perf TEXT);
CREATE TABLE IF NOT EXISTS addresses (address TEXT PRIMARY KEY, sources TEXT, first_seen INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS large_trades (
    hash TEXT, time INTEGER, coin TEXT, px REAL, sz REAL, notional REAL, buyer TEXT, seller TEXT,
    PRIMARY KEY (hash, time, coin, buyer, seller));
CREATE TABLE IF NOT EXISTS fills (
    address TEXT, time INTEGER, tid INTEGER, oid INTEGER, coin TEXT, px TEXT, sz TEXT, data TEXT,
    PRIMARY KEY (address, tid, oid, time, px, sz));
CREATE INDEX IF NOT EXISTS fills_addr_time ON fills (address, time);
CREATE TABLE IF NOT EXISTS ledger (
    address TEXT, time INTEGER, hash TEXT, type TEXT, data TEXT, PRIMARY KEY (address, time, hash, type));
CREATE TABLE IF NOT EXISTS candles (
    coin TEXT, interval TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL, PRIMARY KEY (coin, interval, t));
CREATE TABLE IF NOT EXISTS funding (coin TEXT, time INTEGER, rate REAL, PRIMARY KEY (coin, time));
CREATE TABLE IF NOT EXISTS coverage (
    kind TEXT, key TEXT, covered_from INTEGER, covered_to INTEGER, truncated INTEGER, updated_at INTEGER,
    PRIMARY KEY (kind, key));
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- generic JSON blobs (portfolio, clearinghouseState, meta, role, results) ------------------

    def kv_put(self, kind: str, key: str, data: Any, now: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO kv (kind, key, fetched_at, data) VALUES (?, ?, ?, ?)",
            (kind, key, now, json.dumps(data, separators=(",", ":"))),
        )
        self.db.commit()

    def kv_get(self, kind: str, key: str, *, max_age_ms: float | None = None, now: int | None = None) -> Any | None:
        row = self.db.execute("SELECT fetched_at, data FROM kv WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        if row is None:
            return None
        fetched_at, data = row
        if max_age_ms is not None and now is not None and now - fetched_at > max_age_ms:
            return None
        return json.loads(data)

    def kv_keys(self, kind: str) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT key FROM kv WHERE kind = ?", (kind,))]

    # --- coverage bookkeeping for incremental fetches ------------------------------------------------

    def coverage_get(self, kind: str, key: str) -> tuple[int, int, bool] | None:
        row = self.db.execute(
            "SELECT covered_from, covered_to, truncated FROM coverage WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        return (row[0], row[1], bool(row[2])) if row else None

    def coverage_set(self, kind: str, key: str, covered_from: int, covered_to: int, truncated: bool, now: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO coverage VALUES (?, ?, ?, ?, ?, ?)",
            (kind, key, covered_from, covered_to, int(truncated), now),
        )
        self.db.commit()

    # --- leaderboard & address pool -------------------------------------------------------------------

    def leaderboard_replace(self, rows: Iterable[dict[str, Any]], now: int) -> int:
        self.db.execute("DELETE FROM leaderboard")
        n = 0
        for r in rows:
            self.db.execute(
                "INSERT OR REPLACE INTO leaderboard VALUES (?, ?, ?, ?, ?)",
                (r["address"], now, r["account_value"], r.get("display_name"), json.dumps(r["perf"])),
            )
            n += 1
        self.db.commit()
        return n

    def leaderboard_row(self, address: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM leaderboard WHERE address = ?", (address,)).fetchone()
        if r is None:
            return None
        keys = ("address", "fetched_at", "account_value", "display_name")
        return {**dict(zip(keys, r[:4], strict=True)), "perf": json.loads(r[4])}

    def leaderboard_rows(self) -> list[dict[str, Any]]:
        out = []
        for address, fetched_at, account_value, display_name, perf in self.db.execute("SELECT * FROM leaderboard"):
            out.append(
                {
                    "address": address,
                    "fetched_at": fetched_at,
                    "account_value": account_value,
                    "display_name": display_name,
                    "perf": json.loads(perf),
                }
            )
        return out

    def leaderboard_fetched_at(self) -> int | None:
        row = self.db.execute("SELECT MAX(fetched_at) FROM leaderboard").fetchone()
        return row[0] if row and row[0] is not None else None

    def addresses_add(self, addresses: Iterable[str], source: str, now: int) -> None:
        for addr in addresses:
            row = self.db.execute("SELECT sources FROM addresses WHERE address = ?", (addr,)).fetchone()
            if row is None:
                self.db.execute("INSERT INTO addresses VALUES (?, ?, ?, ?)", (addr, source, now, now))
            else:
                sources = set(row[0].split(",")) | {source}
                self.db.execute(
                    "UPDATE addresses SET sources = ?, last_seen = ? WHERE address = ?",
                    (",".join(sorted(sources)), now, addr),
                )
        self.db.commit()

    def addresses(self) -> dict[str, set[str]]:
        return {a: set(s.split(",")) for a, s in self.db.execute("SELECT address, sources FROM addresses")}

    def large_trades_add(self, trades: Iterable[Any]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO large_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(t.hash, t.time, t.coin, t.px, t.sz, t.notional, t.buyer, t.seller) for t in trades],
        )
        self.db.commit()

    def large_trade_addresses(self, since_ms: int) -> dict[str, float]:
        """address -> largest notional seen since `since_ms`."""
        out: dict[str, float] = {}
        for buyer, seller, notional in self.db.execute(
            "SELECT buyer, seller, notional FROM large_trades WHERE time >= ?", (since_ms,)
        ):
            for a in (buyer, seller):
                out[a] = max(out.get(a, 0.0), notional)
        return out

    # --- fills ------------------------------------------------------------------------------------------

    def fills_put(self, address: str, fills: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    address,
                    int(f["time"]),
                    int(f.get("tid") or 0),
                    int(f.get("oid") or 0),
                    f.get("coin"),
                    str(f.get("px")),
                    str(f.get("sz")),
                    json.dumps(f, separators=(",", ":")),
                )
                for f in fills
            ],
        )
        self.db.commit()

    def fills_get(self, address: str, start_ms: int = 0, end_ms: int = 2**62) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM fills WHERE address = ? AND time >= ? AND time <= ? ORDER BY time, tid",
            (address, start_ms, end_ms),
        )
        return [json.loads(r[0]) for r in rows]

    # --- ledger -----------------------------------------------------------------------------------------

    def ledger_put(self, address: str, updates: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO ledger VALUES (?, ?, ?, ?, ?)",
            [
                (
                    address,
                    int(u["time"]),
                    str(u.get("hash")),
                    str((u.get("delta") or {}).get("type")),
                    json.dumps(u, separators=(",", ":")),
                )
                for u in updates
            ],
        )
        self.db.commit()

    def ledger_get(self, address: str, start_ms: int = 0, end_ms: int = 2**62) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT data FROM ledger WHERE address = ? AND time >= ? AND time <= ? ORDER BY time",
            (address, start_ms, end_ms),
        )
        return [json.loads(r[0]) for r in rows]

    # --- market data --------------------------------------------------------------------------------------

    def candles_put(self, coin: str, interval: str, candles: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (coin, interval, int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c["v"]))
                for c in candles
            ],
        )
        self.db.commit()

    def candles_get(self, coin: str, interval: str, start_ms: int = 0, end_ms: int = 2**62) -> list[tuple]:
        return list(
            self.db.execute(
                "SELECT t, o, h, l, c, v FROM candles WHERE coin = ? AND interval = ? AND t >= ? AND t <= ? ORDER BY t",
                (coin, interval, start_ms, end_ms),
            )
        )

    def funding_put(self, coin: str, rows: Iterable[dict[str, Any]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO funding VALUES (?, ?, ?)",
            [(coin, int(r["time"]), float(r["fundingRate"])) for r in rows],
        )
        self.db.commit()

    def funding_get(self, coin: str, start_ms: int = 0, end_ms: int = 2**62) -> list[tuple[int, float]]:
        return list(
            self.db.execute(
                "SELECT time, rate FROM funding WHERE coin = ? AND time >= ? AND time <= ? ORDER BY time",
                (coin, start_ms, end_ms),
            )
        )

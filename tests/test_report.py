"""End-to-end: a synthetic week in the Parquet store → M1 report (validates the DuckDB SQL)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.analytics.m1_report import bucket_of, build_report
from polybot.core.config import AppConfig, Settings
from polybot.core.timeutil import NS_PER_S, now_ns
from polybot.data.records import Kind, Record, Source
from polybot.data.sink import ParquetSink
from tests.conftest import gamma_page

YES = "1111111111111111111111111111111111111111111111111111111111111111111111111101"
NO = "1111111111111111111111111111111111111111111111111111111111111111111111111102"
CID = "0x" + f"{1:064x}"
S = 1_000_000_000  # one second in ns


def change_frame(ts: int, tops: dict[str, tuple[str, str]]) -> Record:
    changes = [
        {"asset_id": a, "price": "0.5", "size": "1", "side": "BUY", "best_bid": b, "best_ask": k}
        for a, (b, k) in tops.items()
    ]
    payload = {
        "event_type": "price_change",
        "market": CID,
        "price_changes": changes,
        "timestamp": str(ts // 1_000_000 - 40),
    }
    return Record(
        ts,
        Source.CLOB_MARKET_WS,
        Kind.FRAME,
        json.dumps(payload),
        event_type="price_change",
        n_events=1,
        market=CID,
        server_ts_ms=ts // 1_000_000 - 40,
    )


def trade(ts: int, price: str, size: str) -> Record:
    payload = {
        "event_type": "last_trade_price",
        "market": CID,
        "asset_id": YES,
        "price": price,
        "size": size,
        "side": "BUY",
        "timestamp": str(ts // 1_000_000),
    }
    return Record(
        ts,
        Source.CLOB_MARKET_WS,
        Kind.FRAME,
        json.dumps(payload),
        event_type="last_trade_price",
        n_events=1,
        asset_id=YES,
        server_ts_ms=ts // 1_000_000,
    )


def sports(ts: int, live: bool, score: str) -> Record:
    payload = {
        "gameId": 90001,
        "leagueAbbreviation": "atp",
        "status": "InProgress" if live else "Scheduled",
        "live": live,
        "ended": False,
        "score": score,
    }
    return Record(ts, Source.SPORTS_WS, Kind.FRAME, json.dumps(payload), key="90001")


@pytest.fixture
async def store(tmp_path: Path) -> Path:
    start = (now_ns() // NS_PER_S - 3600) * NS_PER_S  # whole seconds: exact ms arithmetic
    sink = ParquetSink(tmp_path / "raw")
    for event in gamma_page(start)["events"]:
        sink.write(
            Record(
                start - 4 * 3600 * NS_PER_S,
                Source.GAMMA_EVENTS,
                Kind.REST,
                json.dumps(event),
                event_type="tennis",
                key=event["id"],
            )
        )
    t_open = start - 3 * 3600 * NS_PER_S
    sink.write(change_frame(t_open, {YES: ("0.48", "0.52"), NO: ("0.48", "0.52")}))
    sink.write(change_frame(t_open + 600 * S, {YES: ("0.49", "0.51"), NO: ("0.49", "0.51")}))
    sink.write(trade(start - 2 * 3600 * NS_PER_S, "0.5", "100"))
    sink.write(sports(start - 600 * S, False, ""))
    sink.write(sports(start + 20 * S, True, "0-0"))
    sink.write(change_frame(start + 60 * S, {YES: ("0.55", "0.57")}))
    sink.write(sports(start + 61 * S, True, "15-0"))
    sink.write(change_frame(start + 300 * S, {YES: ("0", "1"), NO: ("0", "1")}))
    sink.write(trade(start + 600 * S, "0.6", "100"))
    for ms in (80, 90, 100):
        sink.write(
            Record(
                start,
                Source.PROBE_REST,
                Kind.PROBE,
                "{}",
                event_type="clob_time",
                latency_ns=ms * 1_000_000,
            )
        )
    sink.write(
        Record(
            start,
            Source.CLOB_MARKET_WS,
            Kind.PROBE,
            "",
            event_type="ping_rtt",
            latency_ns=70_000_000,
        )
    )
    await sink.close()
    return tmp_path


def test_report_numbers(store: Path, app_config: AppConfig) -> None:
    settings = Settings(_env_file=None, DATA_DIR=str(store))  # type: ignore[call-arg]
    report = build_report(settings, app_config, days=7)
    # Volume: one trade 2 h before the start (6–1h bucket) and one in play.
    assert "| tennis | 6–1ч | 1 | 100 | 50 |" in report
    assert "| tennis | после старта | 1 | 100 | 60 |" in report
    # Spread: 4¢ for 10 min then 2¢ (held at most 30 min), both tokens: 2.5¢ weighted.
    assert "| tennis | 6–1ч | 2.50 | 2.5 | 0% |" in report
    # Network: REST probe p50 = 90 ms; WS ping = 70 ms.
    assert "| REST GET /time (раз в минуту, тёплое соединение), мс | 3 | 90.0 |" in report
    assert "| WS market PING→PONG, мс | 1 | 70.0 |" in report
    # One-way from server timestamps: price changes 40 ms behind, trades at receive time.
    assert "сервер `timestamp` → получение, мс | 6 | 40.0 |" in report
    # Feed latency heuristic: score change 1 s after the 6-tick move.
    assert "| tennis | 1 | +1.00 |" in report
    # Start: first live frame 20 s after gameStartTime; book emptied 5 min after.
    assert "| tennis | 1 | +0.3 |" in report
    assert "| tennis | 1 | +5.0 |" in report
    # YES/NO mirrored at both simultaneous changes.
    assert "зеркальных: 2 (100.0%)" in report
    assert "OddsPapi не записывался" in report


def test_buckets() -> None:
    h = 3600 * NS_PER_S
    assert [bucket_of(x) for x in (30 * h, 10 * h, 2 * h, h // 2, -h)] == [
        ">24ч",
        "24–6ч",
        "6–1ч",
        "1ч–старт",
        "после старта",
    ]

"""`polybot daily`: one report per finished UTC day, then fail-safe raw-data retention."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from polybot.core.config import AppConfig, Settings
from polybot.core.timeutil import NS_PER_S, now_ns, ns_to_date, parse_ts_ns
from polybot.data.records import Kind, Record, Source
from polybot.data.sink import ParquetSink
from polybot.ops import daily
from polybot.ops.daily import raw_days, run_daily
from tests.conftest import gamma_page
from tests.test_report import NO, YES, change_frame, trade

H = 3600 * NS_PER_S
TODAY = ns_to_date(now_ns())
OLD, RECENT = TODAY - timedelta(days=4), TODAY - timedelta(days=1)


def noon(day: date) -> int:
    return parse_ts_ns(f"{day.isoformat()}T12:00:00Z") or 0


async def write_day(sink: ParquetSink, day: date, trade_size: str) -> None:
    start = noon(day)
    for event in gamma_page(start)["events"]:
        sink.write(
            Record(start - 6 * H, Source.GAMMA_EVENTS, Kind.REST, json.dumps(event),
                   event_type="tennis", key=event["id"])
        )  # fmt: skip
    sink.write(change_frame(start - 2 * H, {YES: ("0.48", "0.52"), NO: ("0.48", "0.52")}))
    sink.write(trade(start - H, "0.5", trade_size))
    health = {"memory": {"anon_mb": 150.0, "peak_mb": 190.0}, "sink": {"dropped_rows": 0}}
    sink.write(
        Record(start, Source.RECORDER, Kind.CONTROL, json.dumps(health), event_type="health")
    )
    sink.write(
        Record(start, Source.ODDSPAPI_REST, Kind.REST, "[]", event_type="fixtures",
               endpoint="/fixtures?sportId=12", status=200)
    )  # fmt: skip
    await sink.flush()


@pytest.fixture
async def settings(tmp_path: Path) -> Settings:
    sink = ParquetSink(tmp_path / "raw")
    await write_day(sink, OLD, "100")
    await write_day(sink, RECENT, "7")
    return Settings(_env_file=None, DATA_DIR=str(tmp_path))  # type: ignore[call-arg]


async def test_reports_each_day_then_deletes_only_reported_old_days(
    settings: Settings, app_config: AppConfig
) -> None:
    result = run_daily(settings, app_config)
    assert result.ok and result.reported == [OLD.isoformat(), RECENT.isoformat()]
    reports = settings.data_dir / "reports" / "daily"
    old_report = (reports / f"m1_{OLD.isoformat()}.md").read_text(encoding="utf-8")
    recent_report = (reports / f"m1_{RECENT.isoformat()}.md").read_text(encoding="utf-8")
    assert f"сутки {OLD.isoformat()} UTC" in old_report
    # Each report sees only its own day: the old day's trade is 100 shares, the recent 7.
    assert "| tennis | 1ч–старт | 1 | 100 | 50 |" in old_report
    assert "| tennis | 1ч–старт | 1 | 7 | 4 |" in recent_report
    assert "| 1 | 0 | 150 | 150 | 190 | 0 |" in old_report  # recorder health row
    # keep_raw_days = 2: the day 4 days ago goes (except OddsPapi), yesterday stays.
    assert result.deleted == [OLD.isoformat()]
    raw = settings.data_dir / "raw"
    assert [p.name for p in (raw / f"date={OLD.isoformat()}").iterdir()] == ["source=oddspapi_rest"]
    assert (raw / f"date={RECENT.isoformat()}" / "source=clob_market_ws").is_dir()
    # Idempotent: nothing left to report or delete.
    again = run_daily(settings, app_config)
    assert again.ok and not again.reported and not again.deleted


async def test_failed_report_keeps_the_day(
    settings: Settings, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = daily.build_report

    def flaky(settings: Settings, cfg: AppConfig, *, day: date | None = None) -> str:
        if day == OLD:
            raise RuntimeError("disk hiccup")
        return real(settings, cfg, day=day)

    monkeypatch.setattr(daily, "build_report", flaky)
    result = run_daily(settings, app_config)
    assert not result.ok and OLD.isoformat() in result.failed
    assert result.reported == [RECENT.isoformat()]
    assert result.kept_unreported == [OLD.isoformat()] and not result.deleted
    assert (
        settings.data_dir / "raw" / f"date={OLD.isoformat()}" / "source=clob_market_ws"
    ).is_dir()
    monkeypatch.setattr(daily, "build_report", real)
    retry = run_daily(settings, app_config)  # the next night picks the day up again
    assert retry.reported == [OLD.isoformat()] and retry.deleted == [OLD.isoformat()]


def test_refuses_unfinished_day(settings: Settings, app_config: AppConfig) -> None:
    result = run_daily(settings, app_config, day=TODAY, delete=False)
    assert result.failed == {TODAY.isoformat(): "день не завершён (UTC)"}
    assert raw_days(settings.data_dir / "raw") == [OLD, RECENT]

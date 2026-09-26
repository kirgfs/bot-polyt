"""`polybot daily`: reports for finished UTC days, then raw-data retention (docs/runbook_m1.md §6).

For every finished UTC day on disk without a report: compact it, build the M1 report for
that day into `data/reports/daily/m1_<day>.md` and remember the day in
`data/state/daily_reports.json`. Then delete raw partitions older than
`daily.keep_raw_days` finished days, fail-safe: only days whose report was built, never
today, never the `daily.keep_sources` (tiny; matching needs fixtures from earlier days).

A day whose report fails stays on disk and is retried on the next run.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from polybot.analytics.m1_report import build_report
from polybot.core.config import AppConfig, Settings
from polybot.core.logging import get_logger
from polybot.core.timeutil import now_ns, ns_to_date
from polybot.data.sink import compact_day

log = get_logger(__name__)

STATE_FILE = "daily_reports.json"
REPORTS_DIR = Path("reports") / "daily"


def raw_days(raw: Path) -> list[date]:
    """Dates of the `date=YYYY-MM-DD` partitions under the raw root, ascending."""
    days = []
    for path in raw.glob("date=*"):
        try:
            days.append(date.fromisoformat(path.name.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(days)


class ReportedDays:
    """Days whose daily report was built; retention deletes only these."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8")).get("reported", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            loaded = []
        self.days: set[str] = {str(d) for d in loaded}

    def __contains__(self, day: date) -> bool:
        return day.isoformat() in self.days

    def add(self, day: date) -> None:
        self.days.add(day.isoformat())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"reported": sorted(self.days)}, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)


def delete_day(day_dir: Path, keep_sources: tuple[str, ...]) -> bool:
    """Remove a day's raw partitions except `keep_sources`; True if anything was removed."""
    removed = False
    for source_dir in sorted(day_dir.glob("source=*")):
        if source_dir.name.removeprefix("source=") in keep_sources:
            continue
        shutil.rmtree(source_dir)
        removed = True
    if day_dir.exists() and not any(day_dir.iterdir()):
        day_dir.rmdir()
    return removed


@dataclass
class DailyResult:
    reported: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    kept_unreported: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        def items(values: list[str]) -> str:
            return ", ".join(values) if values else "—"

        lines = [
            f"Отчёты собраны: {items(self.reported)}",
            f"Сырые данные удалены: {items(self.deleted)}",
            f"Не удалены, нет отчёта: {items(self.kept_unreported)}",
        ]
        lines += [f"ОШИБКА {day}: {error}" for day, error in sorted(self.failed.items())]
        return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_daily(
    settings: Settings,
    cfg: AppConfig,
    *,
    day: date | None = None,
    delete: bool = True,
    today: date | None = None,
) -> DailyResult:
    """Report every finished day without a report (or just `day`), then apply retention."""
    raw = settings.data_dir / "raw"
    state = ReportedDays(settings.data_dir / "state" / STATE_FILE)
    today = today or ns_to_date(now_ns())
    finished = [d for d in raw_days(raw) if d < today]
    targets = [day] if day is not None else [d for d in finished if d not in state]
    result = DailyResult()
    for target in targets:
        if target >= today:
            result.failed[target.isoformat()] = "день не завершён (UTC)"
            continue
        try:
            compact_day(raw, target.isoformat())
            text = build_report(settings, cfg, day=target)
            _write_atomic(settings.data_dir / REPORTS_DIR / f"m1_{target.isoformat()}.md", text)
        except Exception as exc:  # one broken day must not block the rest or retention
            log.exception("daily_report_failed", day=target.isoformat())
            result.failed[target.isoformat()] = repr(exc)
            continue
        state.add(target)
        result.reported.append(target.isoformat())
        log.info("daily_report_written", day=target.isoformat())
    if delete:
        daily = cfg.recorder.daily
        cutoff = today - timedelta(days=daily.keep_raw_days)
        for old in (d for d in finished if d < cutoff):
            if old not in state:
                result.kept_unreported.append(old.isoformat())
            elif delete_day(raw / f"date={old.isoformat()}", daily.keep_sources):
                result.deleted.append(old.isoformat())
                log.info("raw_day_deleted", day=old.isoformat())
    return result

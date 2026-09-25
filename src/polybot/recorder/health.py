"""Recorder health: periodic status file (for the Docker healthcheck), log line and Parquet row."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

from polybot.core.logging import get_logger
from polybot.core.timeutil import NS_PER_S, now_ns
from polybot.data.records import Kind, Record, Source
from polybot.data.sink import ParquetSink

log = get_logger(__name__)

STATUS_FILE = "recorder_status.json"
# Log and persist a full snapshot every N status intervals (status file every interval).
SNAPSHOT_EVERY = 10


class Health:
    def __init__(
        self,
        state_dir: Path,
        interval_s: float,
        sink: ParquetSink,
        components: dict[str, Callable[[], dict[str, object]]],
    ) -> None:
        self._path = state_dir / STATUS_FILE
        self._interval_s = interval_s
        self._sink = sink
        self._components = components
        self._started_ns = now_ns()

    def snapshot(self) -> dict[str, object]:
        status: dict[str, object] = {
            "ts_ns": now_ns(),
            "run_id": self._sink.run_id,
            "uptime_s": round((now_ns() - self._started_ns) / NS_PER_S),
            "sink": self._sink.stats.as_dict(),
        }
        for name, getter in self._components.items():
            try:
                status[name] = getter()
            except Exception as exc:  # a broken reporter must not stop the recorder
                status[name] = {"error": repr(exc)}
        return status

    def write_status(self, status: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    async def run(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(self._interval_s)
            status = self.snapshot()
            self.write_status(status)
            tick += 1
            if tick % SNAPSHOT_EVERY == 0:
                log.info("recorder_health", **{k: v for k, v in status.items() if k != "ts_ns"})
                self._sink.write(
                    Record(
                        ts_recv_ns=now_ns(),
                        source=Source.RECORDER,
                        kind=Kind.CONTROL,
                        event_type="health",
                        payload=json.dumps(status, default=str),
                    )
                )


def check_status_file(path: Path, max_age_s: float) -> tuple[bool, str]:
    """Exit criterion for `polybot health` (Docker healthcheck)."""
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"status file unreadable: {exc!r}"
    age_s = (now_ns() - int(status.get("ts_ns", 0))) / NS_PER_S
    if age_s > max_age_s:
        return False, f"status is {age_s:.0f}s old"
    pool = status.get("market_ws")
    if isinstance(pool, dict) and pool.get("assets") and not pool.get("conns_open"):
        return False, "subscribed assets but no open market WS connection"
    sink = status.get("sink")
    if isinstance(sink, dict) and sink.get("dropped_rows"):
        return False, f"sink dropped {sink['dropped_rows']} rows"
    return True, "ok"

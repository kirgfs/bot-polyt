"""Recorder health: periodic status file (for the Docker healthcheck), log lines and Parquet rows.

Every status interval: the status file, with process memory. Every `memory_log_interval_s`:
a `recorder_memory` log line (a warning above `rss_warn_mb`). Every SNAPSHOT_EVERY
intervals: the full status in the log and in Parquet (the report plots memory from it).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

from polybot.core.config import HealthConfig
from polybot.core.logging import get_logger
from polybot.core.memory import process_memory
from polybot.core.timeutil import NS_PER_S, mono_ns, now_ns
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
        cfg: HealthConfig,
        sink: ParquetSink,
        components: dict[str, Callable[[], dict[str, object]]],
    ) -> None:
        self._path = state_dir / STATUS_FILE
        self._cfg = cfg
        self._sink = sink
        self._components = components
        self._started_ns = now_ns()
        self._last_memory_log_mono = 0

    def snapshot(self) -> dict[str, object]:
        status: dict[str, object] = {
            "ts_ns": now_ns(),
            "run_id": self._sink.run_id,
            "uptime_s": round((now_ns() - self._started_ns) / NS_PER_S),
            "memory": process_memory(),
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

    def log_memory(self, status: dict[str, object]) -> None:
        memory = status.get("memory")
        memory = memory if isinstance(memory, dict) else {}
        pool = status.get("market_ws")
        sink = self._sink.stats
        fields = {
            **memory,
            "assets": pool.get("assets") if isinstance(pool, dict) else None,
            "sink_buffered_mb": round(sink.buffered_mb, 1),
            "sink_peak_buffered_mb": round(sink.peak_buffered_mb, 1),
        }
        anon = memory.get("anon_mb", memory.get("rss_mb"))
        if isinstance(anon, float) and anon > self._cfg.rss_warn_mb:
            log.warning("recorder_memory_high", warn_mb=self._cfg.rss_warn_mb, **fields)
        else:
            log.info("recorder_memory", **fields)

    async def run(self) -> None:
        tick = 0
        memory_every_ns = int(self._cfg.memory_log_interval_s * NS_PER_S)
        while True:
            await asyncio.sleep(self._cfg.status_interval_s)
            status = self.snapshot()
            self.write_status(status)
            if mono_ns() - self._last_memory_log_mono >= memory_every_ns:
                self._last_memory_log_mono = mono_ns()
                self.log_memory(status)
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

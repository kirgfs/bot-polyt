"""Process memory telemetry: status file and per-minute log line (recorder OOM guard)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from polybot.core.config import HealthConfig
from polybot.core.memory import process_memory
from polybot.data.sink import ParquetSink
from polybot.recorder.health import Health


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc is Linux-only")
def test_process_memory_reads_proc() -> None:
    memory = process_memory()
    assert set(memory) == {"rss_mb", "anon_mb", "peak_mb"}
    assert 0 < memory["anon_mb"] <= memory["rss_mb"] <= memory["peak_mb"]


def health(tmp_path: Path, **cfg: float) -> Health:
    components = {"market_ws": lambda: {"assets": 7, "conns_open": 1}}
    return Health(tmp_path, HealthConfig(**cfg), ParquetSink(tmp_path / "raw"), components)  # type: ignore[arg-type]


def test_status_file_has_memory(tmp_path: Path) -> None:
    h = health(tmp_path)
    h.write_status(h.snapshot())
    status = json.loads((tmp_path / "recorder_status.json").read_text(encoding="utf-8"))
    assert "memory" in status and status["market_ws"]["assets"] == 7


def test_memory_log_line_and_warning(tmp_path: Path) -> None:
    status = {
        "memory": {"rss_mb": 120.0, "anon_mb": 100.0, "peak_mb": 130.0},
        "market_ws": {"assets": 7},
    }
    with capture_logs() as logs:
        health(tmp_path, rss_warn_mb=250).log_memory(status)
        health(tmp_path, rss_warn_mb=50).log_memory(status)
    assert [e["event"] for e in logs] == ["recorder_memory", "recorder_memory_high"]
    assert (
        logs[0]["anon_mb"] == 100.0 and logs[0]["assets"] == 7 and logs[1]["log_level"] == "warning"
    )

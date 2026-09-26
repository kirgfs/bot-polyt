"""Recorder memory stays flat and under the limit with thousands of markets (OOM regression).

Runs `polybot record` against the local fake Polymarket: ~1,600 match events (thousands of
subscribed tokens, deep books, hundreds of book updates per second), full Gamma snapshots
every minute and constant market churn. Excluded from the default run; `make soak` or
`pytest -m soak`. Duration: SOAK_SECONDS (default 180).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.soak.run_soak import run_soak

LIMIT_MB = 300.0  # user requirement for the VPS
MAX_GROWTH_MB = 20.0


@pytest.mark.soak
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_recorder_memory_is_bounded_and_flat(tmp_path: Path) -> None:
    result = run_soak(duration_s=float(os.environ.get("SOAK_SECONDS", "180")), workdir=tmp_path)
    report = result.render() + "\n" + "\n".join(result.log_tail)
    assert result.exit_code == 0, report
    assert (result.counter("market_ws", "assets") or 0) >= 2000, report
    assert (result.counter("market_ws", "frames") or 0) > 10_000, report
    assert result.counter("sink", "dropped_rows") == 0, report
    assert result.peak_anon_mb < LIMIT_MB, report
    assert result.growth_mb < MAX_GROWTH_MB, report

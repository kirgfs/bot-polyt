"""Soak run: `polybot record` against the local fake Polymarket, memory sampled from /proc.

    python -m tests.soak.run_soak --duration 600
    python -m tests.soak.run_soak --duration 300 --matches tennis=1200,soccer=1600,basketball=400

Linux only (reads /proc/<pid>/status). The recorder runs as a separate process with the
production config except for endpoints and faster discovery/snapshot cadence, so memory
peaks that take an hour in production show up within minutes.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
KB = 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def proc_memory_kb(pid: int) -> dict[str, int]:
    """VmRSS, RssAnon (what the OOM killer reports as anon-rss), RssFile and peak VmHWM."""
    out: dict[str, int] = {}
    with open(f"/proc/{pid}/status", encoding="ascii") as status:
        for line in status:
            key, _, rest = line.partition(":")
            if key in ("VmRSS", "RssAnon", "RssFile", "VmHWM"):
                out[key] = int(rest.split()[0])
    return out


def write_config(dst: Path, http_url: str, ws_url: str, sports_url: str) -> None:
    base = yaml.safe_load((REPO / "config" / "base.yaml").read_text(encoding="utf-8"))
    base["geoblock"]["url"] = f"{http_url}/api/geoblock"
    base["polymarket"] = {
        "clob_url": http_url,
        "gamma_url": http_url,
        "market_ws_url": f"{ws_url}/ws/market",
        "sports_ws_url": f"{sports_url}/ws",
    }
    rec = yaml.safe_load((REPO / "config" / "recorder.yaml").read_text(encoding="utf-8"))
    # Compress time: discovery every 15 s and a full Gamma snapshot every minute
    # (production: 120 s and hourly); rewards every 2 minutes (production: hourly).
    rec["discovery"]["interval_s"] = 15
    rec["discovery"]["full_snapshot_interval_s"] = 60
    rec["clob_meta"]["rewards_interval_s"] = 120
    rec["health"]["status_interval_s"] = 5
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "base.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")
    (dst / "recorder.yaml").write_text(yaml.safe_dump(rec), encoding="utf-8")


def wait_http(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            if time.monotonic() > deadline:
                raise
        time.sleep(0.2)


@dataclass
class Sample:
    t_s: float
    anon_mb: float
    rss_mb: float
    status: dict[str, Any] = field(default_factory=dict)


@dataclass
class SoakResult:
    duration_s: float
    samples: list[Sample]
    exit_code: int | None
    status: dict[str, Any]
    log_tail: list[str]

    @property
    def peak_anon_mb(self) -> float:
        return max((s.anon_mb for s in self.samples), default=0.0)

    @property
    def peak_rss_mb(self) -> float:
        return max((s.rss_mb for s in self.samples), default=0.0)

    def window(self, lo: float, hi: float) -> list[float]:
        return [
            s.anon_mb for s in self.samples if lo * self.duration_s <= s.t_s < hi * self.duration_s
        ]

    @property
    def growth_mb(self) -> float:
        """Median anon RSS of the last third minus that of the middle third.

        Medians, not peaks: periodic bursts (Gamma snapshots, rewards pages) come and go;
        a leak moves the baseline.
        """
        middle, last = self.window(1 / 3, 2 / 3), self.window(2 / 3, 1.01)
        if not middle or not last:
            return 0.0
        return statistics.median(last) - statistics.median(middle)

    def counter(self, *path: str) -> Any:
        node: Any = self.status
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return node

    def render(self) -> str:
        lines = [
            "| t, с | anon RSS, МБ | RSS, МБ | активов | соединений | кадров | строк в Parquet |",
            "|---|---|---|---|---|---|---|",
        ]
        step = max(1, len(self.samples) // 12)
        for sample in self.samples[::step] + self.samples[-1:]:
            ws = sample.status.get("market_ws", {}) if sample.status else {}
            sink = sample.status.get("sink", {}) if sample.status else {}
            rows = sum((sink.get("rows_written") or {}).values()) if sink else 0
            lines.append(
                f"| {sample.t_s:.0f} | {sample.anon_mb:.0f} | {sample.rss_mb:.0f} | "
                f"{ws.get('assets', '')} | {ws.get('conns_open', '')} | {ws.get('frames', '')} | {rows} |"
            )
        lines += [
            "",
            f"- пик anon RSS: {self.peak_anon_mb:.0f} МБ, пик RSS: {self.peak_rss_mb:.0f} МБ",
            f"- рост (медиана последней трети − медиана средней трети): {self.growth_mb:+.1f} МБ",
            f"- код выхода рекордера: {self.exit_code}",
            f"- sink: {json.dumps(self.counter('sink'), ensure_ascii=False)}",
        ]
        return "\n".join(lines)


def run_soak(
    *,
    duration_s: float,
    workdir: Path,
    matches: str = "tennis=600,soccer=800,basketball=200",
    rate: float = 800.0,
    levels: int = 40,
    extra_env: dict[str, str] | None = None,
) -> SoakResult:
    http_port, ws_port, sports_port = free_port(), free_port(), free_port()
    config_dir, data_dir = workdir / "config", workdir / "data"
    write_config(
        config_dir,
        f"http://127.0.0.1:{http_port}",
        f"ws://127.0.0.1:{ws_port}",
        f"ws://127.0.0.1:{sports_port}",
    )
    env = {
        **os.environ,
        "MALLOC_ARENA_MAX": "2",  # as in the Dockerfile
        "DATA_DIR": str(data_dir),
        "CONFIG_DIR": str(config_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "PYTHONPATH": str(REPO / "src"),
        **(extra_env or {}),
    }
    fake_cmd = [
        sys.executable,
        "-m",
        "tests.soak.fake_polymarket",
        "--http-port",
        str(http_port),
        "--ws-port",
        str(ws_port),
        "--sports-port",
        str(sports_port),
        "--matches",
        matches,
        "--rate",
        str(rate),
        "--levels",
        str(levels),
    ]
    log_path = workdir / "recorder.log"
    fake = subprocess.Popen(fake_cmd, cwd=REPO, stdout=subprocess.DEVNULL)
    recorder: subprocess.Popen[bytes] | None = None
    samples: list[Sample] = []
    status: dict[str, Any] = {}
    try:
        wait_http(f"http://127.0.0.1:{http_port}/time")
        with log_path.open("wb") as log:
            recorder = subprocess.Popen(
                [sys.executable, "-m", "polybot", "record"],
                cwd=workdir,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            status_path = data_dir / "state" / "recorder_status.json"
            start = time.monotonic()
            while (elapsed := time.monotonic() - start) < duration_s:
                if recorder.poll() is not None:
                    break
                mem = proc_memory_kb(recorder.pid)
                with contextlib.suppress(OSError, json.JSONDecodeError):
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                samples.append(
                    Sample(elapsed, mem.get("RssAnon", 0) / KB, mem.get("VmRSS", 0) / KB, status)
                )
                time.sleep(1.0)
            if recorder.poll() is None:
                recorder.send_signal(signal.SIGTERM)
            exit_code = recorder.wait(timeout=60)
    finally:
        if recorder is not None and recorder.poll() is None:
            recorder.kill()
        fake.terminate()
        fake.wait(timeout=10)
    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
    with (workdir / "samples.csv").open("w", encoding="utf-8") as out:
        out.write("t_s,anon_mb,rss_mb\n")
        out.writelines(f"{s.t_s:.1f},{s.anon_mb:.1f},{s.rss_mb:.1f}\n" for s in samples)
    return SoakResult(duration_s, samples, exit_code, status, tail)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--matches", default="tennis=600,soccer=800,basketball=200")
    parser.add_argument("--rate", type=float, default=800.0)
    parser.add_argument("--levels", type=int, default=40)
    parser.add_argument("--workdir", type=Path, default=REPO / "data" / "soak")
    parser.add_argument("--limit-mb", type=float, default=300.0)
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)
    result = run_soak(
        duration_s=args.duration,
        workdir=args.workdir,
        matches=args.matches,
        rate=args.rate,
        levels=args.levels,
    )
    print(result.render())
    ok = result.peak_anon_mb < args.limit_mb and result.exit_code == 0
    if not ok:
        print("\n".join(result.log_tail))
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

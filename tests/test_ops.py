from __future__ import annotations

import json
from pathlib import Path

import pytest

from polybot.__main__ import build_parser, main
from polybot.analytics.stats import quantile, summarize
from polybot.core.timeutil import NS_PER_S, now_ns
from polybot.ops.netcheck import HostCheck, _is_bogon, _verdict
from polybot.recorder.health import check_status_file
from polybot.venues.polymarket.clob_rest import cf_colo


class TestHealth:
    def write(self, path: Path, **status: object) -> None:
        path.write_text(json.dumps({"ts_ns": now_ns(), **status}), encoding="utf-8")

    def test_fresh_ok(self, tmp_path: Path) -> None:
        self.write(
            tmp_path / "s.json", market_ws={"assets": 10, "conns_open": 1}, sink={"dropped_rows": 0}
        )
        assert check_status_file(tmp_path / "s.json", 60) == (True, "ok")

    def test_stale(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"ts_ns": now_ns() - 600 * NS_PER_S}), encoding="utf-8")
        ok, reason = check_status_file(path, 60)
        assert not ok and "old" in reason

    def test_no_open_connection(self, tmp_path: Path) -> None:
        self.write(tmp_path / "s.json", market_ws={"assets": 10, "conns_open": 0})
        assert not check_status_file(tmp_path / "s.json", 60)[0]

    def test_missing_file(self, tmp_path: Path) -> None:
        assert not check_status_file(tmp_path / "nope.json", 60)[0]


class TestNetcheck:
    def test_bogon(self) -> None:
        assert _is_bogon("10.0.0.1") and _is_bogon("127.0.0.1") and _is_bogon("garbage")
        assert not _is_bogon("104.18.1.1")

    def check(self, **fields: object) -> HostCheck:
        base: dict[str, object] = {
            "label": "clob",
            "url": "https://clob.polymarket.com/time",
            "host": "clob.polymarket.com",
        }
        base.update(fields)
        return HostCheck(**base)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            (
                {"dns_error": "gaierror", "doh_ips": ["1.1.1.1"]},
                "DNS blocked (system resolver fails, DoH resolves)",
            ),
            ({"system_ips": ["10.10.10.10"]}, "DNS tampering (private/bogon address)"),
            ({"system_ips": ["104.18.1.1"]}, "TCP blocked or unreachable"),
            (
                {"system_ips": ["104.18.1.1"], "tcp_ms": 5.0, "tls_error": "reset"},
                "TLS failure (DPI/interception?)",
            ),
            (
                {
                    "system_ips": ["104.18.1.1"],
                    "tcp_ms": 5.0,
                    "notes": ["HTTP 451: blocked for legal reasons"],
                },
                "possible block page",
            ),
            ({"system_ips": ["104.18.1.1"], "tcp_ms": 5.0, "status": 200}, "ok"),
        ],
    )
    def test_verdicts(self, fields: dict[str, object], expected: str) -> None:
        assert _verdict(self.check(**fields)) == expected


def test_cf_colo() -> None:
    assert cf_colo({"cf-ray": "8c1d2e3f4a5b6c7d-FRA"}) == "FRA"
    assert cf_colo({}) is None


def test_quantiles() -> None:
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    summary = summarize([5.0, 1.0, 3.0])
    assert (summary.n, summary.p50, summary.min, summary.max) == (3, 3.0, 1.0, 5.0)
    assert summarize([]).row()[0] == "0"


def test_cli_parser_and_health_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = build_parser().parse_args(["oddspapi-eval", "burst", "--duration", "60"])
    assert args.step == "burst" and args.duration == 60.0
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["health", "--max-age", "10"]) == 1

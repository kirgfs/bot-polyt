from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from polybot.core.config import AppConfig, load_config
from polybot.core.timeutil import ns_to_datetime
from polybot.data.records import Record

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent


class ListWriter:
    """RecordWriter that keeps records in memory."""

    def __init__(self) -> None:
        self.records: list[Record] = []

    def write(self, record: Record) -> None:
        self.records.append(record)

    def of(self, source: str, kind: str | None = None) -> list[Record]:
        return [r for r in self.records if r.source == source and (kind is None or r.kind == kind)]


@pytest.fixture
def writer() -> ListWriter:
    return ListWriter()


@pytest.fixture
def app_config() -> AppConfig:
    return load_config(REPO / "config")


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def gamma_page(start_ns: int) -> dict[str, Any]:
    """The Gamma fixture with every match start moved to `start_ns` (keeps the raw format)."""
    page = copy.deepcopy(load_json("gamma_events_tennis.json"))
    stamp = ns_to_datetime(start_ns).strftime("%Y-%m-%d %H:%M:%S+00")
    for event in page["events"]:
        for market in event.get("markets", []):
            if market.get("gameStartTime"):
                market["gameStartTime"] = stamp
    return dict(page)

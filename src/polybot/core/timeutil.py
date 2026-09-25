"""Time helpers. Wall clock and timestamps are int nanoseconds UTC (CLAUDE.md)."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

NS_PER_S = 1_000_000_000
NS_PER_MS = 1_000_000

# Epoch magnitudes used to guess the unit of a bare numeric timestamp.
_MS_THRESHOLD = 10**11  # year 5138 in seconds, 1973 in milliseconds
_NS_THRESHOLD = 10**17


def now_ns() -> int:
    return time.time_ns()


def mono_ns() -> int:
    return time.monotonic_ns()


def ns_to_datetime(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / NS_PER_S, tz=UTC)


def ns_to_date(ts_ns: int) -> date:
    return ns_to_datetime(ts_ns).date()


def ns_to_iso(ts_ns: int) -> str:
    return ns_to_datetime(ts_ns).isoformat().replace("+00:00", "Z")


def _epoch_to_ns(magnitude: float) -> int:
    if magnitude >= _NS_THRESHOLD:
        return int(magnitude)
    if magnitude >= _MS_THRESHOLD:
        return int(magnitude * NS_PER_MS)
    return int(magnitude * NS_PER_S)


def parse_ts_ns(value: object) -> int | None:
    """Parse an API timestamp into int ns UTC; None when absent or unparseable.

    Accepts ISO 8601 with ``Z`` or an offset, including Gamma's
    ``"2026-08-18 01:15:00+00"`` (docs/api_notes.md §11), and epoch
    seconds/ms/ns as a number or digit string. Naive datetimes are rejected:
    guessing the timezone of a start time is exactly the error we must not make.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _epoch_to_ns(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdecimal():
        return _epoch_to_ns(int(text))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp()) * NS_PER_S + parsed.microsecond * 1000

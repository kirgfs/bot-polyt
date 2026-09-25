"""Small statistics helpers (no numpy dependency)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default 'linear' method)."""
    if not sorted_values:
        return math.nan
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(sorted_values[low])
    weight = position - low
    return float(sorted_values[low]) * (1 - weight) + float(sorted_values[high]) * weight


@dataclass(frozen=True, slots=True)
class Summary:
    n: int
    p50: float
    p95: float
    p99: float
    min: float
    max: float

    def row(self, fmt: str = "{:.1f}") -> list[str]:
        if self.n == 0:
            return ["0", "—", "—", "—", "—", "—"]
        return [
            str(self.n),
            fmt.format(self.p50),
            fmt.format(self.p95),
            fmt.format(self.p99),
            fmt.format(self.min),
            fmt.format(self.max),
        ]


def summarize(values: Iterable[float]) -> Summary:
    data = sorted(v for v in values if not math.isnan(v))
    if not data:
        return Summary(0, math.nan, math.nan, math.nan, math.nan, math.nan)
    return Summary(
        n=len(data),
        p50=quantile(data, 0.50),
        p95=quantile(data, 0.95),
        p99=quantile(data, 0.99),
        min=data[0],
        max=data[-1],
    )


def md_table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(str(c) for c in row) + " |" for row in rows)
    return "\n".join(lines)

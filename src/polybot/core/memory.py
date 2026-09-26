"""Process memory for health output and logs: the recorder must stay far from the OOM killer."""

from __future__ import annotations

import sys
from pathlib import Path

_STATUS = Path("/proc/self/status")
# /proc/self/status keys → our names. RssAnon is what the kernel OOM report calls anon-rss.
_FIELDS = {"VmRSS": "rss_mb", "RssAnon": "anon_mb", "VmHWM": "peak_mb"}


def process_memory() -> dict[str, float]:
    """Resident memory in MB: `rss_mb`, `anon_mb`, `peak_mb` (peak RSS since start).

    Linux (VPS, Docker) reads /proc/self/status. Elsewhere only the peak is available
    (resource module); on Windows, where the code is developed, the dict is empty.
    """
    try:
        text = _STATUS.read_text(encoding="ascii")
    except OSError:
        return _peak_only()
    out: dict[str, float] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        name = _FIELDS.get(key)
        if name is not None:
            out[name] = round(int(rest.split()[0]) / 1024, 1)
    return out


def _peak_only() -> dict[str, float]:
    try:
        import resource  # noqa: PLC0415 - not available on Windows
    except ImportError:
        return {}
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024  # bytes on macOS, KiB on Linux
    return {"peak_mb": round(peak / divisor, 1)}

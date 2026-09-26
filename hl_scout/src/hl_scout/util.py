"""Small shared helpers: time, addresses, rounding."""

from __future__ import annotations

import math
import re
import time

MS = 1
SEC = 1000 * MS
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def is_address(text: str) -> bool:
    return bool(_ADDRESS_RE.match(text.strip()))


def norm_address(text: str) -> str:
    """Lower-case 0x address; raises ValueError for anything else."""
    addr = text.strip()
    if not is_address(addr):
        raise ValueError(f"не адрес Hyperliquid: {text!r}")
    return addr.lower()


def short_address(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def explorer_url(addr: str) -> str:
    """Official explorer page of a wallet [api_notes §1]."""
    return f"https://app.hyperliquid.xyz/explorer/address/{addr}"


def floor_sig(x: float, digits: int = 2) -> float:
    """Round DOWN to `digits` significant figures (copy ratio must never round up)."""
    if x <= 0 or not math.isfinite(x):
        return 0.0
    exp = math.floor(math.log10(x)) - digits + 1
    step = 10.0**exp
    return round(math.floor(x / step + 1e-9) * step, max(0, -exp))


def round_size_down(size: float, sz_decimals: int) -> float:
    """Order sizes are truncated to the asset's szDecimals [api_notes §2]."""
    step = 10.0 ** (-sz_decimals)
    return math.floor(abs(size) / step + 1e-9) * step


def is_perp_coin(coin: str, allow_hip3: bool = False) -> bool:
    """Main-dex perps only by default: spot is '@N' or 'A/B', HIP-4 outcome markets are '#N', HIP-3 perps are
    'dex:COIN' [api_notes §2]."""
    if coin.startswith(("@", "#")) or "/" in coin:
        return False
    if ":" in coin:
        return allow_hip3
    return True


def fmt_usd(x: float) -> str:
    sign = "−" if x < 0 else ""
    x = abs(x)
    if x >= 1e9:
        return f"{sign}${x / 1e9:.2f}B"
    if x >= 1e6:
        return f"{sign}${x / 1e6:.2f}M"
    if x >= 1e4:
        return f"{sign}${x / 1e3:.1f}k"
    if x >= 100:
        return f"{sign}${x:,.0f}"
    return f"{sign}${x:,.2f}"


def fmt_pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def fmt_dur_ms(ms: float) -> str:
    minutes = ms / MIN
    if minutes < 90:
        return f"{minutes:.0f} мин"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f} ч"
    return f"{hours / 24:.1f} дн"

"""Read-only view of a Hyperliquid account by its PUBLIC address — no keys, nothing is signed.

- `resolve_address`: which account an address really is (`userRole`). An API wallet (agent) holds nothing and
  its queries come back empty — the master account is used instead [api_notes §2, §8a].
- `fetch_account`: my copy account as the copy bot sees it — equity, margin, positions with distance to
  liquidation, and the API wallets (agents) approved for it with their expiry [api_notes §8a].
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from hl_scout.hl.client import InfoClient
from hl_scout.util import DAY, fmt_usd, norm_address, now_ms


class AddressError(ValueError):
    """The address has no account on Hyperliquid."""


@dataclass(frozen=True)
class ResolvedAddress:
    given: str
    address: str  # the account to read (master for an agent)
    role: str
    master: str | None = None
    note: str | None = None


def resolve_role(given: str, role: Any) -> ResolvedAddress:
    """Pure part of `resolve_address` (unit-tested without network)."""
    addr = norm_address(given)
    r = str((role or {}).get("role", "missing"))
    data = (role or {}).get("data") or {}
    if r == "missing":
        raise AddressError(f"{addr}: аккаунта на Hyperliquid нет (userRole = missing)")
    if r == "agent":
        master = str(data.get("user", "")).lower()
        if not master:
            raise AddressError(f"{addr}: это API-кошелёк (агент), но мастер-аккаунт не указан")
        return ResolvedAddress(
            addr,
            master,
            r,
            master,
            f"{addr} — API-кошелёк (агент): он ничего не хранит, читаем мастер-аккаунт {master}",
        )
    if r == "subAccount":
        master = str(data.get("master", "")).lower() or None
        return ResolvedAddress(addr, addr, r, master, f"субаккаунт мастер-аккаунта {master}")
    if r == "vault":
        return ResolvedAddress(addr, addr, r, None, "это vault: скаут не рекомендует vault-адреса")
    return ResolvedAddress(addr, addr, r)


async def resolve_address(client: InfoClient, given: str) -> ResolvedAddress:
    addr = norm_address(given)
    return resolve_role(addr, await client.user_role(addr))


@dataclass(frozen=True)
class PositionView:
    coin: str
    size: float
    entry_px: float
    mark_px: float
    value: float
    upnl: float
    liq_px: float | None
    leverage: str

    @property
    def liq_distance(self) -> float:
        if self.liq_px is None or self.mark_px <= 0:
            return math.inf
        return abs(self.mark_px - self.liq_px) / self.mark_px


@dataclass(frozen=True)
class AgentView:
    name: str
    address: str
    valid_until: int | None

    def days_left(self, now: int) -> float | None:
        return None if self.valid_until is None else (self.valid_until - now) / DAY


@dataclass
class AccountSnapshot:
    address: str
    time: int
    account_value: float
    withdrawable: float
    margin_used: float
    total_notional: float
    spot_usdc: float
    positions: list[PositionView] = field(default_factory=list)
    agents: list[AgentView] = field(default_factory=list)

    @property
    def free_margin(self) -> float:
        return self.account_value - self.margin_used


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def parse_account(address: str, clearinghouse: Any, spot: Any, mids: Any, agents: Any, now: int) -> AccountSnapshot:
    ch = clearinghouse if isinstance(clearinghouse, dict) else {}
    ms = ch.get("marginSummary") or {}
    mids = mids if isinstance(mids, dict) else {}
    positions = []
    for ap in ch.get("assetPositions") or []:
        p = ap.get("position") or {}
        size = _f(p.get("szi"))
        if size == 0:
            continue
        coin = str(p.get("coin"))
        value = abs(_f(p.get("positionValue")))
        mark = _f(mids.get(coin), value / abs(size) if size else 0.0)
        lev = p.get("leverage") or {}
        liq = p.get("liquidationPx")
        positions.append(
            PositionView(
                coin=coin,
                size=size,
                entry_px=_f(p.get("entryPx")),
                mark_px=mark,
                value=value,
                upnl=_f(p.get("unrealizedPnl")),
                liq_px=_f(liq) if liq not in (None, "") else None,
                leverage=f"{lev.get('value', '?')}x {lev.get('type', '')}".strip(),
            )
        )
    usdc = sum(_f(b.get("total")) for b in (spot or {}).get("balances") or [] if b.get("coin") == "USDC")
    agent_views = [
        AgentView(
            str(a.get("name") or "(без имени)"),
            str(a.get("address", "")).lower(),
            int(a["validUntil"]) if a.get("validUntil") not in (None, "") else None,
        )
        for a in agents or []
        if isinstance(a, dict)
    ]
    return AccountSnapshot(
        address=address,
        time=now,
        account_value=_f(ms.get("accountValue")),
        withdrawable=_f(ch.get("withdrawable")),
        margin_used=_f(ms.get("totalMarginUsed")),
        total_notional=_f(ms.get("totalNtlPos")),
        spot_usdc=usdc,
        positions=positions,
        agents=agent_views,
    )


async def fetch_account(client: InfoClient, address: str) -> AccountSnapshot:
    """4 cheap public requests: clearinghouseState (2), spotClearinghouseState (2), allMids (2), extraAgents (20)."""
    ch = await client.clearinghouse_state(address)
    spot = await client.spot_clearinghouse_state(address)
    mids = await client.all_mids()
    agents = await client.extra_agents(address)
    return parse_account(address, ch, spot, mids, agents, now_ms())


def account_warnings(s: AccountSnapshot, *, agent_warn_days: float = 7.0, liq_warn: float = 0.10) -> list[str]:
    out = []
    for p in s.positions:
        if p.liq_distance < liq_warn:
            out.append(f"{p.coin}: до ликвидации {p.liq_distance:.1%}")
    for a in s.agents:
        left = a.days_left(s.time)
        if left is not None and left < 0:
            out.append(f"API-кошелёк «{a.name}» просрочен — copy-бот с ним не торгует")
        elif left is not None and left < agent_warn_days:
            out.append(f"API-кошелёк «{a.name}» истекает через {left:.1f} дн — продлите в copy-боте")
    if s.account_value > 0 and s.margin_used / s.account_value > 0.8:
        out.append(f"занято {s.margin_used / s.account_value:.0%} маржи")
    return out


def render_account(s: AccountSnapshot, resolved: ResolvedAddress | None = None) -> str:
    lines = [f"Аккаунт {s.address}"]
    if resolved and resolved.note:
        lines.append(f"  ⓘ {resolved.note}")
    lines += [
        f"  Equity (perp): {fmt_usd(s.account_value)}, маржа занята {fmt_usd(s.margin_used)}, "
        f"свободно {fmt_usd(s.free_margin)}, к выводу {fmt_usd(s.withdrawable)}, USDC в споте {fmt_usd(s.spot_usdc)}",
        f"  Позиции ({len(s.positions)}), notional {fmt_usd(s.total_notional)}:",
    ]
    for p in s.positions:
        side = "лонг" if p.size > 0 else "шорт"
        liq = "—" if not math.isfinite(p.liq_distance) else f"{p.liq_distance:.1%}"
        lines.append(
            f"    {p.coin} {side} {abs(p.size):g} @ {p.entry_px:g} (сейчас {p.mark_px:g}), "
            f"PnL {fmt_usd(p.upnl)}, {p.leverage}, до ликвидации {liq}"
        )
    if not s.positions:
        lines.append("    нет")
    lines.append(f"  API-кошельки (агенты): {len(s.agents)}")
    for a in s.agents:
        left = a.days_left(s.time)
        lines.append(f"    «{a.name}» {a.address} — " + ("бессрочно" if left is None else f"ещё {left:.0f} дн"))
    warns = account_warnings(s)
    if warns:
        lines.append("  ⚠ " + "; ".join(warns))
    return "\n".join(lines)

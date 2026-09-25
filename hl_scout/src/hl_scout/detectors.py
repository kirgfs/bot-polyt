"""Behaviour detectors: martingale, market maker, liquidation bot, hedges, linked wallets, suspicious spikes."""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np

from hl_scout.analytics import Action, Fill, Trip
from hl_scout.market import SPOT_ALIASES
from hl_scout.util import DAY, HOUR


@dataclass(frozen=True)
class MartingaleStats:
    trip_share: float  # trips with at least one add into a loss
    max_loss_adds: int
    escalating: bool  # ≥ 2 loss adds with non-decreasing sizes in one trip


def martingale_stats(trips: list[Trip]) -> MartingaleStats:
    if not trips:
        return MartingaleStats(0.0, 0, False)
    with_adds = [t for t in trips if t.loss_adds > 0]
    escalating = any(
        len(t.loss_add_sizes) >= 2
        and all(b >= a * 0.999 for a, b in zip(t.loss_add_sizes, t.loss_add_sizes[1:], strict=False))
        for t in with_adds
    )
    return MartingaleStats(
        trip_share=len(with_adds) / len(trips),
        max_loss_adds=max((t.loss_adds for t in trips), default=0),
        escalating=escalating,
    )


def maker_stats(fills: list[Fill], t0: int, t1: int) -> tuple[float, float]:
    """(maker share of notional, fills per day) inside [t0, t1)."""
    w = [f for f in fills if t0 <= f.t < t1]
    if not w:
        return 0.0, 0.0
    total = sum(f.notional for f in w)
    maker = sum(f.notional for f in w if not f.crossed)
    days = max((t1 - t0) / DAY, 1e-9)
    return (maker / total if total > 0 else 0.0), len(w) / days


def liquidation_stats(fills: list[Fill], t0: int, t1: int) -> tuple[int, float]:
    """(own liquidation events, share of fills that were the counterparty to someone's liquidation)."""
    w = [f for f in fills if t0 <= f.t < t1]
    if not w:
        return 0, 0.0
    own_events = {(f.coin, f.t) for f in w if f.own_liq}
    counter = sum(1 for f in w if f.liq_counterparty)
    return len(own_events), counter / len(w)


def opposite_positions_share(actions: list[Action], t0: int, t1: int, min_ratio: float) -> float:
    """Share of in-market time with long and short legs of comparable size (pair trades / delta-neutral)."""
    pos: dict[str, float] = {}
    px: dict[str, float] = {}
    hedged = in_market = 0.0
    prev_t = t0

    def account(until: int) -> None:
        nonlocal hedged, in_market
        span = max(0, min(until, t1) - max(prev_t, t0))
        if span <= 0:
            return
        long_g = sum(p * px[c] for c, p in pos.items() if p > 0)
        short_g = sum(-p * px[c] for c, p in pos.items() if p < 0)
        if long_g > 0 or short_g > 0:
            in_market += span
            if long_g > 0 and short_g > 0 and min(long_g, short_g) / max(long_g, short_g) >= min_ratio:
                hedged += span

    for a in actions:
        if a.t >= t1:
            break
        account(a.t)
        prev_t = max(prev_t, a.t)
        pos[a.coin] = a.pos_after
        px[a.coin] = a.px
    account(t1)
    return hedged / in_market if in_market > 0 else 0.0


def spot_hedge_ratio(clearinghouse: Any, spot_state: Any, spot_prices: dict[str, float]) -> float:
    """Max over coins of (spot holding value) / (short perp notional) in the same asset, current state."""
    if not isinstance(clearinghouse, dict) or not isinstance(spot_state, dict):
        return 0.0
    spot_value: dict[str, float] = defaultdict(float)
    for b in spot_state.get("balances") or []:
        name = SPOT_ALIASES.get(str(b.get("coin")), str(b.get("coin")))
        try:
            total = float(b.get("total") or 0.0)
        except (TypeError, ValueError):
            continue
        price = spot_prices.get(name)
        if price and total > 0 and name not in ("USDC", "USDH", "USDT0", "USDE"):
            spot_value[name] += total * price
    ratio = 0.0
    for ap in clearinghouse.get("assetPositions") or []:
        p = ap.get("position") or {}
        try:
            szi = float(p.get("szi") or 0.0)
            value = abs(float(p.get("positionValue") or 0.0))
        except (TypeError, ValueError):
            continue
        coin = str(p.get("coin"))
        if szi < 0 and value > 0 and spot_value.get(coin, 0.0) > 0:
            ratio = max(ratio, spot_value[coin] / value)
    return ratio


def funding_pnl_share(trips: list[Trip], min_hold_ms: int = 24 * HOUR) -> float:
    """Share of total PnL (with funding) that came from funding on long-held positions."""
    total = sum(t.pnl_with_funding for t in trips)
    if total <= 0:
        return 0.0
    carry = sum(t.funding for t in trips if t.hold_ms >= min_hold_ms and t.funding > 0)
    return carry / total


def small_add_share(trips: list[Trip]) -> float:
    """Share of trader actions that are small adds (< 25% of the position) — the $10 minimum drops them."""
    actions = sum(len(t.actions) for t in trips)
    return sum(t.small_adds for t in trips) / actions if actions else 0.0


def weekly_spike_z(weekly: np.ndarray, min_history: int = 4) -> float:
    """z-score of the latest weekly return vs the wallet's earlier weeks; +inf when there is no history."""
    if len(weekly) == 0:
        return 0.0
    hist = weekly[:-1]
    if len(hist) < min_history:
        return float("inf") if weekly[-1] > 0 else 0.0
    sd = float(np.std(hist, ddof=1))
    if sd <= 0:
        return float("inf") if weekly[-1] > float(np.mean(hist)) else 0.0
    return (float(weekly[-1]) - float(np.mean(hist))) / sd


# --------------------------------------------------------------------------------------------------------
# Linked wallets
# --------------------------------------------------------------------------------------------------------


def sync_similarity(a: list[Action], b: list[Action], window_ms: int) -> tuple[int, float]:
    """Actions of `a` matched by an action of `b` with the same coin and side within ±window."""
    if not a or not b:
        return 0, 0.0
    index: dict[tuple[str, int], list[int]] = defaultdict(list)
    for x in b:
        index[(x.coin, x.side)].append(x.t)
    for v in index.values():
        v.sort()
    matches = 0
    for x in a:
        ts = index.get((x.coin, x.side))
        if not ts:
            continue
        i = bisect.bisect_left(ts, x.t - window_ms)
        if i < len(ts) and ts[i] <= x.t + window_ms:
            matches += 1
    return matches, matches / min(len(a), len(b))


_TRANSFER_TYPES = {"internalTransfer", "subAccountTransfer", "send", "spotTransfer"}


def transfer_counterparties(address: str, ledger: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    me = address.lower()
    for u in ledger:
        d = u.get("delta") or {}
        if d.get("type") not in _TRANSFER_TYPES:
            continue
        for k in ("user", "destination"):
            other = str(d.get(k) or "").lower()
            if other.startswith("0x") and other != me:
                out.add(other)
    return out


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass(frozen=True)
class Link:
    a: str
    b: str
    reason: str


def find_links(
    actions: dict[str, list[Action]],
    ledgers: dict[str, list[dict[str, Any]]],
    masters: dict[str, str],
    *,
    sync_window_s: float,
    sync_share: float,
    min_matches: int,
    max_counterparty_degree: int,
) -> list[Link]:
    addrs = sorted(actions)
    links: list[Link] = []
    for a, b in combinations(addrs, 2):
        m, share = sync_similarity(actions[a], actions[b], int(sync_window_s * 1000))
        if m >= min_matches and share >= sync_share:
            links.append(Link(a, b, f"синхронные сделки: {m} совпадений ({share:.0%})"))
    pool = set(addrs)
    by_counterparty: dict[str, set[str]] = defaultdict(set)
    for addr in addrs:
        for cp in transfer_counterparties(addr, ledgers.get(addr, [])):
            if cp in pool:
                links.append(Link(min(addr, cp), max(addr, cp), "прямой перевод между кошельками"))
            else:
                by_counterparty[cp].add(addr)
    for cp, members in by_counterparty.items():
        if 2 <= len(members) <= max_counterparty_degree:
            for a, b in combinations(sorted(members), 2):
                links.append(Link(a, b, f"общий источник переводов {cp[:10]}…"))
    for sub, master in masters.items():
        if master in pool and sub in pool:
            links.append(Link(min(sub, master), max(sub, master), "субаккаунт"))
    return links


def clusters(addresses: list[str], links: list[Link]) -> dict[str, str]:
    """address → cluster representative (lowest address of the connected component)."""
    uf = _UnionFind(addresses)
    for link in links:
        if link.a in uf.parent and link.b in uf.parent:
            uf.union(link.a, link.b)
    return {a: uf.find(a) for a in addresses}

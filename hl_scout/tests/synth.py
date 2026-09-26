"""Synthetic market and wallets in the exact JSON shapes of the Info API (docs/api_notes.md §2).

Used only by tests: no network. Prices follow a seeded random walk; a trader's skill is modelled by the
probability of entering in the direction of the next price move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hl_scout.hl.client import INTERVAL_MS
from hl_scout.market import Bars, Funding, MarketData, parse_meta
from hl_scout.scoring import WalletData
from hl_scout.util import DAY, HOUR, MIN

T_END = 1_790_000_000_000  # fixed "now" (2026-09-21) for deterministic tests
COINS = {"BTC": (60_000.0, 5, 40), "ETH": (3_000.0, 4, 25), "SOL": (150.0, 2, 20), "DOGE": (0.2, 0, 10)}


@dataclass
class World:
    t_start: int
    t_end: int
    minute_t: np.ndarray
    prices: dict[str, np.ndarray]  # 1-minute closes
    meta_json: list[Any]
    candles: dict[str, dict[str, list[tuple]]] = field(default_factory=dict)
    funding_rate: float = 0.0000125

    def price(self, coin: str, t: int) -> float:
        i = int(np.clip((t - self.t_start) // MIN, 0, len(self.minute_t) - 1))
        return float(self.prices[coin][i])

    def market(self) -> MarketData:
        meta = parse_meta(self.meta_json)
        bars = {c: Bars.merge(self.candles[c]) for c in self.prices}
        ft = np.arange(self.t_start + HOUR, self.t_end, HOUR, dtype=np.int64)
        funding = {c: Funding(ft, np.full(len(ft), self.funding_rate)) for c in self.prices}
        return MarketData(meta=meta, bars=bars, funding=funding, spot_prices={})


def make_world(
    seed: int = 1,
    days: int = 200,
    vol_daily: float = 0.03,
    t_end: int = T_END,
    fine_days: tuple[float, float] = (52.0, 3.5),
) -> World:
    rng = np.random.default_rng(seed)
    t_start = t_end - days * DAY
    n = days * 24 * 60
    minute_t = t_start + MIN * np.arange(n, dtype=np.int64)
    sigma = vol_daily / math.sqrt(24 * 60)
    prices = {}
    for coin, (p0, _, _) in COINS.items():
        steps = rng.normal(0.0, sigma, n)
        prices[coin] = p0 * np.exp(np.cumsum(steps))
    universe = [
        {"name": c, "szDecimals": d, "maxLeverage": lev, "marginTableId": lev} for c, (_, d, lev) in COINS.items()
    ]
    ctxs = [
        {"markPx": str(prices[c][-1]), "dayNtlVlm": str(1e9 if c in ("BTC", "ETH") else 2e7), "funding": "0.0000125"}
        for c in COINS
    ]
    world = World(t_start, t_end, minute_t, prices, [{"universe": universe, "marginTables": []}, ctxs])
    fine_15m, fine_1m = fine_days
    for coin in COINS:
        world.candles[coin] = {
            "1h": _resample(minute_t, prices[coin], 60, t_start),
            "15m": _resample(minute_t, prices[coin], 15, t_end - int(fine_15m * DAY)),
            "1m": _resample(minute_t, prices[coin], 1, t_end - int(fine_1m * DAY)),
        }
    return world


def _resample(minute_t: np.ndarray, px: np.ndarray, k: int, t_from: int) -> list[tuple]:
    start = int(np.searchsorted(minute_t, t_from))
    start -= start % k
    out = []
    step = INTERVAL_MS[{1: "1m", 15: "15m", 60: "1h"}[k]]
    for i in range(start, len(px) - k + 1, k):
        seg = px[max(i - 1, 0) : i + k]
        o = float(px[i - 1]) if i > 0 else float(px[i])
        out.append((int(minute_t[i]), o, float(seg.max()), float(seg.min()), float(px[i + k - 1]), 1.0))
        assert int(minute_t[i]) % step == int(minute_t[start]) % step
    return out


@dataclass
class TraderSpec:
    address: str
    skill: float = 0.62  # P(enter in the direction of the next move)
    trades_per_day: float = 1.5
    hold_min: tuple[float, float] = (60.0, 12 * 60.0)
    coins: tuple[str, ...] = ("BTC", "ETH")
    notional: float = 20_000.0
    equity: float = 50_000.0
    adds: int = 0  # extra adds per trade (ladder)
    add_into_loss: bool = False  # martingale: adds only when losing, growing size
    maker: bool = False


def make_wallet(world: World, spec: TraderSpec, seed: int = 7) -> WalletData:
    rng = np.random.default_rng(seed)
    fills: list[dict[str, Any]] = []
    t = world.t_start + 2 * HOUR
    tid = oid = 1
    realized = 0.0
    events: list[tuple[int, float]] = []  # (time, realized pnl delta incl. fees)
    open_until: dict[str, int] = {}
    while t < world.t_end - 2 * HOUR:
        t += int(rng.exponential(DAY / spec.trades_per_day))
        if t >= world.t_end - 2 * HOUR:
            break
        coin = str(rng.choice(spec.coins))
        if open_until.get(coin, 0) > t:
            continue
        hold = int(rng.uniform(*spec.hold_min) * MIN)
        t_close = min(t + hold, world.t_end - HOUR)
        p_open, p_close = world.price(coin, t), world.price(coin, t_close)
        future_up = p_close > p_open
        direction = (1 if future_up else -1) if rng.random() < spec.skill else (-1 if future_up else 1)
        sz = spec.notional / p_open
        pos = 0.0
        entry_cost = 0.0

        def add_fill(
            tt: int, side: int, size: float, px: float, closed: float, start_pos: float, coin: str = coin
        ) -> None:
            nonlocal tid, oid
            fee = abs(size * px) * (0.00015 if spec.maker else 0.00045)
            fills.append(
                {
                    "coin": coin,
                    "px": f"{px:.6f}",
                    "sz": f"{size:.6f}",
                    "side": "B" if side > 0 else "A",
                    "time": tt,
                    "startPosition": f"{start_pos:.6f}",
                    "dir": "",
                    "closedPnl": f"{closed:.6f}",
                    "hash": "0x" + "0" * 64,
                    "oid": oid,
                    "crossed": not spec.maker,
                    "fee": f"{fee:.6f}",
                    "tid": tid,
                    "feeToken": "USDC",
                    "twapId": None,
                }
            )
            tid += 1
            oid += 1
            events.append((tt, closed - fee))

        add_fill(t, direction, sz, p_open, 0.0, pos)
        pos += direction * sz
        entry_cost += sz * p_open
        for k in range(spec.adds):
            ta = t + (k + 1) * hold // (spec.adds + 2)
            pa = world.price(coin, ta)
            losing = (pa - entry_cost / abs(pos)) * direction < 0
            if spec.add_into_loss and not losing:
                continue
            size = sz * (1.5 ** (k + 1) if spec.add_into_loss else 0.3)
            add_fill(ta, direction, size, pa, 0.0, pos)
            pos += direction * size
            entry_cost += size * pa
        entry = entry_cost / abs(pos)
        closed = (p_close - entry) * abs(pos) * direction
        add_fill(t_close, -direction, abs(pos), p_close, closed, pos)
        realized += closed
        open_until[coin] = t_close + MIN
        t = max(t, t_close) if len(spec.coins) == 1 else t
    fills.sort(key=lambda f: (f["time"], f["tid"]))
    portfolio = _portfolio(world, spec.equity, events)
    return WalletData(
        address=spec.address,
        raw_fills=fills,
        fills_truncated=False,
        portfolio=portfolio,
        clearinghouse={"assetPositions": [], "marginSummary": {"accountValue": str(spec.equity)}},
        spot_state={"balances": []},
        ledger=[{"time": world.t_start, "hash": "0x1", "delta": {"type": "deposit", "usdc": str(spec.equity)}}],
        role={"role": "user"},
        history_from=world.t_start,
    )


def _portfolio(world: World, equity0: float, events: list[tuple[int, float]]) -> list[Any]:
    """Hourly account value = start equity + realized PnL (unrealized ignored: fine for tests)."""
    ev = sorted(events)
    ev_t = np.array([e[0] for e in ev], dtype=np.int64)
    cum = np.cumsum([e[1] for e in ev]) if ev else np.zeros(0)
    hours = np.arange(world.t_start, world.t_end + 1, HOUR, dtype=np.int64)

    def series(t0: int) -> dict[str, Any]:
        ts = hours[hours >= t0]
        idx = np.searchsorted(ev_t, ts, side="right") - 1
        pnl = np.where(idx >= 0, cum[np.clip(idx, 0, None)] if len(cum) else 0.0, 0.0)
        base = pnl[0] if len(pnl) else 0.0
        return {
            "accountValueHistory": [[int(t), f"{equity0 + p:.2f}"] for t, p in zip(ts, pnl, strict=True)],
            "pnlHistory": [[int(t), f"{p - base:.2f}"] for t, p in zip(ts, pnl, strict=True)],
            "vlm": "0.0",
        }

    windows = {
        "allTime": world.t_start,
        "month": world.t_end - 30 * DAY,
        "week": world.t_end - 7 * DAY,
        "day": world.t_end - DAY,
    }
    out: list[Any] = []
    for name, t0 in windows.items():
        out.append([name, series(t0)])
    for name, t0 in windows.items():
        out.append(["perp" + name[0].upper() + name[1:], series(t0)])
    return out

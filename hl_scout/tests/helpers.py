"""Helpers for deterministic tests: hand-made price bars, markets and trader actions."""

from __future__ import annotations

import numpy as np

from hl_scout.analytics import Action
from hl_scout.config import CopySemantics
from hl_scout.market import Bars, CoinMeta, Funding, MarketData
from hl_scout.sim import SimEnv
from hl_scout.util import HOUR, MIN

T0 = 1_780_000_000_000 - (1_780_000_000_000 % HOUR)


def flat_then(prices: list[float], start: int = T0, step: int = MIN) -> Bars:
    """1-minute bars from a list of closes (open = previous close, high/low = max/min of both)."""
    p = np.asarray(prices, dtype=float)
    o = np.concatenate([[p[0]], p[:-1]])
    t0 = start + step * np.arange(len(p), dtype=np.int64)
    return Bars(t0, t0 + step, o, np.maximum(o, p), np.minimum(o, p), p)


def make_market(bars: dict[str, Bars], funding_rate: float = 0.0, max_lev: int = 40, sz_dec: int = 5) -> MarketData:
    meta = {c: CoinMeta(c, sz_dec, max_lev, day_volume=1e9) for c in bars}
    funding = {}
    for c, b in bars.items():
        ft = np.arange(b.start - b.start % HOUR + HOUR, b.end, HOUR, dtype=np.int64)
        funding[c] = Funding(ft, np.full(len(ft), funding_rate))
    return MarketData(meta=meta, bars=bars, funding=funding, spot_prices={})


def act(t: int, coin: str, side: int, sz: float, px: float, before: float, crossed: bool = True) -> Action:
    return Action(
        t=t,
        t_last=t,
        coin=coin,
        side=side,
        sz=sz,
        px=px,
        pos_before=before,
        pos_after=before + side * sz,
        crossed=crossed,
        maker_share=0.0 if crossed else 1.0,
        closed_pnl=0.0,
        fee=0.0,
        own_liq=False,
        n_fills=1,
    )


def env(
    market: MarketData,
    *,
    fee_bps: float = 0.0,
    bot_bps: float = 0.0,
    slip: float = 0.0,
    delay_s: float = 0.0,
    stop_slip: float = 0.0,
    semantics: CopySemantics | None = None,
    k: float = 0.0,
) -> SimEnv:
    return SimEnv(
        delay_ms=int(delay_s * 1000),
        taker_fee=fee_bps / 1e4,
        bot_fee=bot_bps / 1e4,
        slippage=dict.fromkeys(market.meta, slip),
        delay_penalty_k=k,
        stop_slippage=stop_slip,
        semantics=semantics or CopySemantics(),
    )

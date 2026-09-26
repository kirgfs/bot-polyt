from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from hl_scout.analytics import Action, EquityCurve
from hl_scout.config import Config
from hl_scout.discovery import select_pool
from hl_scout.market import mtm_pnl_curve
from hl_scout.util import HOUR, MIN

from helpers import T0, act, flat_then, make_market


def _act(t, coin, side, sz, px, before, fee=0.0, closed=0.0) -> Action:
    return dataclasses.replace(act(t, coin, side, sz, px, before), fee=fee, closed_pnl=closed)


def _cfg(**discovery) -> Config:
    cfg = Config()
    return cfg.model_copy(update={"discovery": cfg.discovery.model_copy(update=discovery)})


def _base(av: float = 1000.0) -> EquityCurve:
    return EquityCurve(np.array([T0, T0 + 6 * HOUR]), np.array([av, av]), np.zeros(2), "perp")


def test_mtm_curve_marks_open_position_hourly_and_counts_fees():
    # hourly bars: marks at T0+1h..T0+6h are 100, 102, 104, 103, 105, 110
    bars = flat_then([100, 102, 104, 103, 105, 110], step=HOUR)
    market = make_market({"BTC": bars})
    acts = [
        _act(T0 + HOUR + 30 * MIN, "BTC", 1, 2, 101, 0, fee=0.1),
        _act(T0 + 3 * HOUR + 30 * MIN, "BTC", -1, 2, 104.5, 2, fee=0.1),
    ]
    curve, unpriced = mtm_pnl_curve(acts, market, _base(), T0, T0 + 6 * HOUR)
    assert unpriced == set()
    assert list(curve.t) == [T0 + k * HOUR for k in range(1, 7)]
    # 1h flat; 2h, 3h open and marked to the candle; from 4h realized 2·3.5 − 0.2
    assert curve.pnl == pytest.approx([0.0, 1.9, 5.9, 6.8, 6.8, 6.8])
    assert curve.av == pytest.approx([1000.0] * 6)


def test_mtm_curve_funding_position_before_start_and_unpriced_coin():
    bars = flat_then([100, 102, 104, 103, 105, 110], step=HOUR)
    market = make_market({"BTC": bars, "ETH": bars}, funding_rate=0.001)
    acts = [
        _act(T0 - 30 * MIN, "ETH", 1, 1, 99, 0),  # before the window: position 1 carried into it
        _act(T0 + HOUR + 30 * MIN, "BTC", 1, 2, 101, 0),
        _act(T0 + 3 * HOUR + 30 * MIN, "BTC", -1, 2, 104.5, 2),
        _act(T0 + 2 * HOUR + 10 * MIN, "XYZ", -1, 1, 5, 1, fee=0.5, closed=5.0),  # no candles
    ]
    curve, unpriced = mtm_pnl_curve(acts, market, _base(), T0, T0 + 6 * HOUR)
    assert unpriced == {"XYZ"}
    btc_price = [0.0, 2.0, 6.0, 7.0, 7.0, 7.0]
    btc_funding = [0.0, -0.204, -0.412, -0.412, -0.412, -0.412]  # long 2 pays at 2h (102) and 3h (104)
    marks = [100, 102, 104, 103, 105, 110]
    paid = np.cumsum([0.001 * m for m in marks[:5]] + [0.0])  # funding events at 1h…5h (make_market)
    eth = [(m - 100) - paid[k] for k, m in enumerate(marks)]  # long 1 carried from before T0
    xyz = [0.0, 0.0, 4.5, 4.5, 4.5, 4.5]  # realized only
    expected = np.array(btc_price) + np.array(btc_funding) + np.array(eth) + np.array(xyz)
    assert curve.pnl == pytest.approx(expected)


def _row(i: int, av: float, m_pnl: float, m_roi: float, m_vlm: float, all_pnl: float) -> dict:
    return {
        "address": f"0x{i:040x}",
        "account_value": av,
        "perf": {
            "month": {"pnl": m_pnl, "roi": m_roi, "vlm": m_vlm},
            "allTime": {"pnl": all_pnl, "roi": 0.1, "vlm": 10 * m_vlm},
        },
    }


def test_select_pool_band_search_and_seeded_control():
    cfg = _cfg(pool_search=5, pool_control=6, pool_max=14)
    mm = [_row(900 + i, 5e6, 1e5, 0.02, 5e9, 1e7) for i in range(3)]  # outside the band: billions of turnover
    band_win = [_row(i, 20_000, 1_000 * (i + 1), 0.05 * (i + 1), 1e6, 5_000) for i in range(10)]  # roi .05….5
    band_lose = [_row(100 + i, 20_000, -500, -0.02, 1e6, -100) for i in range(10)]
    lottery = [_row(200, 20_000, 60_000, 3.0, 1e6, 60_000)]  # month ROI 300% > pool_max_month_roi
    rows = mm + band_win + band_lose + lottery
    manual = ["0x" + "e" * 40]
    extra = {"0x" + "f" * 40: 1e6}

    pool, control = select_pool(rows, cfg, extra, manual)
    pool2, control2 = select_pool(list(reversed(rows)), cfg, extra, manual)
    assert (pool, control) == (pool2, control2)  # deterministic, independent of the input order

    mm_addr = {r["address"] for r in mm}
    band = {r["address"] for r in band_win + band_lose + lottery}
    assert not mm_addr & set(pool)
    assert len(control) == 6 and control <= band  # sampled without looking at profit
    assert pool[0] == manual[0]
    assert set(pool[1:7]) == control
    search = [a for a in pool[7:] if a not in control and a != "0x" + "f" * 40]
    top_roi = [r["address"] for r in sorted(band_win, key=lambda r: -r["perf"]["month"]["roi"])]
    assert search == [a for a in top_roi if a not in control][: len(search)]
    assert lottery[0]["address"] not in search and not any(a in search for a in (r["address"] for r in band_lose))
    assert len(pool) <= cfg.discovery.pool_max


def test_select_pool_large_trades_are_cut_first():
    cfg = _cfg(pool_search=3, pool_control=3, pool_max=6)
    rows = [_row(i, 20_000, 1_000, 0.1 + i / 100, 1e6, 5_000) for i in range(10)]
    extra = {f"0x{0xABC0 + i:040x}": 1e6 for i in range(5)}
    pool, control = select_pool(rows, cfg, extra, [])
    band = [a for a in pool if a not in extra]
    top3 = {r["address"] for r in sorted(rows, key=lambda r: -r["perf"]["month"]["roi"])[:3]}
    assert set(band) == control | top3  # control and search are never cut in favour of large-trade addresses
    assert len(pool) == 6 and pool[: len(band)] == band

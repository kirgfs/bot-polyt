from __future__ import annotations

import numpy as np
import pytest

from hl_scout.analytics import (
    aggregate_actions,
    build_equity_curve,
    build_trips,
    closed_trips,
    concurrency_profile,
    split_fills,
    weekly_returns,
)
from hl_scout.util import DAY, HOUR, MIN

from helpers import T0

ADDR = "0x" + "a" * 40


def fill(t, coin, side, px, sz, start, closed=0.0, oid=1, tid=None, crossed=True, fee=0.0, **extra):
    d = {
        "time": t,
        "coin": coin,
        "side": side,
        "px": str(px),
        "sz": str(sz),
        "startPosition": str(start),
        "closedPnl": str(closed),
        "oid": oid,
        "tid": tid or t,
        "crossed": crossed,
        "fee": str(fee),
        "dir": "",
        "hash": "0x",
    }
    d.update(extra)
    return d


def test_split_fills_separates_spot_and_hip3():
    raw = [
        fill(T0, "BTC", "B", 100, 1, 0),
        fill(T0, "@107", "B", 1, 1, 0),
        fill(T0, "xyz:AAPL", "B", 1, 1, 0),
        fill(T0, "PURR/USDC", "A", 1, 1, 0),
    ]
    perp, spot = split_fills(raw, ADDR)
    assert [f.coin for f in perp] == ["BTC"]
    assert {f.coin for f in spot} == {"@107", "PURR/USDC"}
    perp_all, _ = split_fills(raw, ADDR, allow_hip3=True)
    assert {f.coin for f in perp_all} == {"BTC", "xyz:AAPL"}


def test_liquidation_flags():
    raw = [
        fill(T0, "BTC", "A", 100, 1, 1, liquidation={"liquidatedUser": ADDR, "markPx": "100", "method": "market"}),
        fill(
            T0 + 1,
            "ETH",
            "B",
            10,
            1,
            0,
            liquidation={"liquidatedUser": "0x" + "b" * 40, "markPx": "10", "method": "market"},
        ),
    ]
    perp, _ = split_fills(raw, ADDR)
    assert perp[0].own_liq and not perp[0].liq_counterparty
    assert perp[1].liq_counterparty and not perp[1].own_liq


def test_actions_aggregate_one_order_and_classify():
    raw = [
        fill(T0, "BTC", "B", 100, 1, 0, oid=1, tid=1),
        fill(T0 + 500, "BTC", "B", 102, 1, 1, oid=1, tid=2),  # same order, 0.5 s later → same action
        fill(T0 + HOUR, "BTC", "B", 105, 1, 2, oid=2),  # increase
        fill(T0 + 2 * HOUR, "BTC", "A", 110, 1, 3, closed=7, oid=3),  # reduce
        fill(T0 + 3 * HOUR, "BTC", "A", 108, 4, 2, closed=10, oid=4),  # flip to short 2
        fill(T0 + 4 * HOUR, "BTC", "B", 100, 2, -2, closed=16, oid=5),  # close
    ]
    perp, _ = split_fills(raw, ADDR)
    acts = aggregate_actions(perp, 2000)
    assert [a.kind for a in acts] == ["open", "increase", "reduce", "flip", "close"]
    assert acts[0].sz == 2 and acts[0].px == pytest.approx(101)


def test_trips_pnl_hold_and_loss_adds():
    raw = [
        fill(T0, "ETH", "B", 100, 1, 0, oid=1, fee=0.1),
        fill(T0 + HOUR, "ETH", "B", 95, 1, 1, oid=2, fee=0.1),  # add into a 5% loss
        fill(T0 + 5 * HOUR, "ETH", "A", 110, 2, 2, closed=25, oid=3, fee=0.2),
        fill(T0 + 6 * HOUR, "ETH", "A", 100, 1, 0, oid=4),
        fill(T0 + 7 * HOUR, "ETH", "B", 90, 1, -1, closed=10, oid=5),
    ]
    perp, _ = split_fills(raw, ADDR)
    trips = build_trips(aggregate_actions(perp), adverse_pct=0.01)
    assert len(trips) == 2
    long_trip, short_trip = trips
    assert long_trip.direction == 1 and short_trip.direction == -1
    assert long_trip.pnl == pytest.approx(25 - 0.4)
    assert long_trip.loss_adds == 1 and long_trip.loss_add_times == [T0 + HOUR]
    assert long_trip.hold_ms == 5 * HOUR
    assert long_trip.entry_px == pytest.approx(97.5)
    assert closed_trips(trips, T0, T0 + DAY) == trips


def test_position_open_before_history_is_marked_incomplete():
    raw = [fill(T0, "SOL", "A", 20, 5, 5, closed=10, oid=1)]  # closing a position we never saw opening
    perp, _ = split_fills(raw, ADDR)
    trips = build_trips(aggregate_actions(perp))
    assert trips[0].pre_existing
    assert closed_trips(trips, 0, T0 + DAY) == []


def test_concurrency_profile():
    raw = [
        fill(T0, "BTC", "B", 1, 1, 0, oid=1),
        fill(T0 + HOUR, "ETH", "B", 1, 1, 0, oid=2),
        fill(T0 + 2 * HOUR, "BTC", "A", 1, 1, 1, oid=3),
        fill(T0 + 3 * HOUR, "ETH", "A", 1, 1, 1, oid=4),
    ]
    perp, _ = split_fills(raw, ADDR)
    med, p95, in_mkt = concurrency_profile(aggregate_actions(perp), T0, T0 + 4 * HOUR)
    # 1 position for 1h, 2 for 1h, 1 for 1h, flat for 1h
    assert med == 1 and p95 == 2 and in_mkt == pytest.approx(0.75)


def _portfolio(points_all, points_month):
    def window(pts):
        return {
            "accountValueHistory": [[t, str(av)] for t, av, _ in pts],
            "pnlHistory": [[t, str(p)] for t, _, p in pts],
            "vlm": "0",
        }

    return [
        ["allTime", window(points_all)],
        ["month", window(points_month)],
        ["perpAllTime", window(points_all)],
        ["perpMonth", window(points_month)],
    ]


def test_equity_curve_stitches_windows_and_ignores_deposits():
    all_pts = [(T0 + i * DAY, 1000 + i * 10, i * 10) for i in range(40)]
    # a 5000 deposit at day 35 changes account value but not PnL
    all_pts = [(t, av + (5000 if t >= T0 + 35 * DAY else 0), p) for t, av, p in all_pts]
    month = [(t, av, p - 100) for t, av, p in all_pts if t >= T0 + 10 * DAY]  # month window PnL restarts near 0
    curve = build_equity_curve(_portfolio(all_pts, month))
    assert curve.pnl_at(T0 + 39 * DAY) == pytest.approx(390)
    r = curve.daily_returns(T0 + 30 * DAY, T0 + 39 * DAY)
    assert (r > 0).all() and r.max() < 0.02  # the deposit is not a return
    assert curve.max_drawdown(T0, T0 + 39 * DAY) == pytest.approx(0.0)


def test_max_drawdown_on_time_weighted_index():
    pnl = [0, 100, 50, -50, 0]
    pts = [(T0 + i * DAY, 1000 + p, p) for i, p in enumerate(pnl)]
    curve = build_equity_curve(_portfolio(pts, pts))
    dd = curve.max_drawdown(T0, T0 + 4 * DAY)
    # 1100 → 950 through two steps: (1 − 50/1100)(1 − 100/1050) − 1
    assert dd == pytest.approx(1 - (1 - 50 / 1100) * (1 - 100 / 1050))


def test_weekly_returns_align_to_the_end():
    daily = np.full(15, 0.01)
    w = weekly_returns(daily)
    assert len(w) == 2 and w[0] == pytest.approx(1.01**7 - 1)


def test_unified_account_falls_back_to_total_curve():
    perp = [(T0 + i * DAY, 10, i) for i in range(5)]
    total = [(T0 + i * DAY, 10_000, i) for i in range(5)]

    def window(pts):
        return {
            "accountValueHistory": [[t, str(av)] for t, av, _ in pts],
            "pnlHistory": [[t, str(p)] for t, _, p in pts],
        }

    pf = [["allTime", window(total)], ["perpAllTime", window(perp)]]
    assert build_equity_curve(pf).basis == "total"
    assert MIN > 0

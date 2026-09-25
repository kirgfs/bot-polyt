from __future__ import annotations

import numpy as np
import pytest

from hl_scout.analytics import Action, Trip
from hl_scout.detectors import (
    clusters,
    find_links,
    martingale_stats,
    opposite_positions_share,
    sync_similarity,
    weekly_spike_z,
)
from hl_scout.montecarlo import simulate_capital
from hl_scout.stats import (
    benjamini_hochberg,
    bootstrap_pvalue,
    deflated_sharpe,
    expected_max_sharpe,
    profit_factor,
    sortino,
)
from hl_scout.util import HOUR, MIN, floor_sig, round_size_down

from helpers import T0


def action(t, coin, side, pos_before, pos_after, px=100.0):
    return Action(
        t, t, coin, side, abs(pos_after - pos_before), px, pos_before, pos_after, True, 0.0, 0.0, 0.0, False, 1
    )


def test_martingale_detection():
    ok = Trip("BTC", 1, T0)
    ok.loss_adds = 0
    bad = Trip("ETH", 1, T0)
    bad.loss_adds, bad.loss_add_sizes = 2, [1.0, 1.5]
    st = martingale_stats([ok, bad])
    assert st.trip_share == 0.5 and st.max_loss_adds == 2 and st.escalating


def test_opposite_positions_share():
    acts = [
        action(T0, "BTC", 1, 0, 1),
        action(T0, "ETH", -1, 0, -1),
        action(T0 + HOUR, "ETH", 1, -1, 0),
        action(T0 + 2 * HOUR, "BTC", -1, 1, 0),
    ]
    # hedged (BTC long + ETH short, same notional) for 1h out of 2h in market
    assert opposite_positions_share(acts, T0, T0 + 3 * HOUR, 0.5) == pytest.approx(0.5)


def test_sync_similarity_and_clusters():
    a = [action(T0 + i * HOUR, "BTC", 1, 0, 1) for i in range(12)]
    b = [action(T0 + i * HOUR + 5_000, "BTC", 1, 0, 1) for i in range(12)]
    c = [action(T0 + i * HOUR + 20 * MIN, "BTC", 1, 0, 1) for i in range(12)]
    assert sync_similarity(a, b, 30_000) == (12, 1.0)
    assert sync_similarity(a, c, 30_000)[0] == 0
    links = find_links(
        {"0xa": a, "0xb": b, "0xc": c},
        {},
        {},
        sync_window_s=30,
        sync_share=0.5,
        min_matches=10,
        max_counterparty_degree=3,
    )
    mapping = clusters(["0xa", "0xb", "0xc"], links)
    assert mapping["0xa"] == mapping["0xb"] != mapping["0xc"]


def test_transfer_links_ignore_service_addresses():
    def tr(src, dst):
        return {
            "time": T0,
            "hash": "0x",
            "delta": {"type": "internalTransfer", "usdc": "10", "user": src, "destination": dst},
        }

    service = "0x" + "f" * 40
    ledgers = {f"0x{i}": [tr(service, f"0x{i}")] for i in range(5)}  # a service funds 5 wallets → not an owner
    ledgers["0x0"].append(tr("0xowner", "0x0"))
    ledgers["0x1"].append(tr("0xowner", "0x1"))
    links = find_links(
        {a: [] for a in ledgers},
        ledgers,
        {},
        sync_window_s=30,
        sync_share=0.5,
        min_matches=10,
        max_counterparty_degree=3,
    )
    assert {(x.a, x.b) for x in links} == {("0x0", "0x1")}


def test_weekly_spike():
    assert weekly_spike_z(np.array([0.01, 0.0, 0.02, 0.01, 0.30])) > 2.5
    assert weekly_spike_z(np.array([0.30])) == float("inf")


def test_sortino_pf_and_dsr_behave():
    rng = np.random.default_rng(0)
    good = rng.normal(0.004, 0.01, 90)
    noise = rng.normal(0.0, 0.01, 90)
    assert sortino(good) > sortino(noise)
    assert profit_factor([10, -5, 5]) == pytest.approx(3.0)
    assert deflated_sharpe(good, 0.01, 50) > deflated_sharpe(noise, 0.01, 50)
    assert expected_max_sharpe(0.01, 100) > expected_max_sharpe(0.01, 10) > 0
    assert deflated_sharpe(good, 0.01, 1000) < deflated_sharpe(good, 0.01, 2)  # more tries → less credit
    assert bootstrap_pvalue(good) < 0.05 < bootstrap_pvalue(noise)


def test_benjamini_hochberg():
    q = benjamini_hochberg([0.01, 0.04, 0.03, 0.5])
    assert q[0] == pytest.approx(0.04) and q[3] == pytest.approx(0.5)
    assert all(qi >= pi for qi, pi in zip(q, [0.01, 0.04, 0.03, 0.5], strict=True))


def test_monte_carlo_basics():
    flat = simulate_capital(np.zeros(30), 50, 30, 1000, 3, 1)
    assert flat.median == 50 and flat.p_ruin == 0 and flat.p_ge[1000.0] == 0
    crash = simulate_capital(np.array([-0.2] * 30), 50, 30, 1000, 3, 1, stop_level=30)
    assert crash.p_stop == 1.0 and crash.median == pytest.approx(50 * 0.8**3)  # frozen after the stop day
    # even +10% every single day for 30 days is not enough for $1000 (×17.4 → $872); it takes ≈ +10.5% a day
    ten = simulate_capital(np.array([0.10] * 30), 50, 30, 500, 3, 1, levels=(100.0, 1000.0))
    assert ten.p_ge[100.0] == 1.0 and ten.p_ge[1000.0] == 0.0
    assert ten.median == pytest.approx(50 * 1.1**30)
    enough = simulate_capital(np.array([0.106] * 30), 50, 30, 500, 3, 1, levels=(1000.0,))
    assert enough.p_ge[1000.0] == 1.0


def test_rounding_helpers():
    assert floor_sig(0.0026789) == 0.0026
    assert floor_sig(123.9) == 120
    assert round_size_down(0.123456, 3) == pytest.approx(0.123)

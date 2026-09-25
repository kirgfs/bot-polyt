from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from hl_scout.config import BacktestCfg, Config, GridCfg, MonteCarloCfg
from hl_scout.market import build_regime

from synth import TraderSpec, make_wallet, make_world


@pytest.fixture(scope="session")
def world():
    return make_world(seed=1, days=160)


@pytest.fixture(scope="session")
def market(world):
    m = world.market()
    m.regime = build_regime(world.candles["BTC"]["1h"])
    return m


@pytest.fixture(scope="session")
def fast_cfg() -> Config:
    """Smaller grid and fewer Monte Carlo paths: same logic, faster tests."""
    grid = GridCfg(
        target_position_usd=[15, 30, 60], leverage=[1, 3, 5], buy_times=[0], small_size=["skip"], price_sl=["none"]
    )
    return Config(backtest=BacktestCfg(grid=grid, tune_mc_paths=300), montecarlo=MonteCarloCfg(paths=2000))


@pytest.fixture(scope="session")
def good_wallet(world):
    spec = TraderSpec(
        "0x" + "1" * 40, skill=0.72, trades_per_day=1.0, hold_min=(240, 2880), notional=15_000, equity=40_000
    )
    return make_wallet(world, spec, seed=5)

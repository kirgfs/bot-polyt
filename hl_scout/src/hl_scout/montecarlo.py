"""Monte Carlo of my capital: block bootstrap of daily copy returns (out-of-sample where possible)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class McResult:
    start: float
    horizon_days: int
    paths: int
    sample_days: int
    median: float
    mean: float
    p5: float
    p95: float
    p_ge: dict[float, float]  # P(final ≥ level)
    p_loss: float  # P(final < start · (1 − loss_threshold))
    p_ruin: float  # P(capital ever < ruin_usd)
    p_stop: float  # P(Balance SL level reached)
    loss_threshold: float

    @property
    def reliable(self) -> bool:
        """Fewer than ~4 weeks of daily returns cannot say much about 30-day tails."""
        return self.sample_days >= 28


def sample_paths(r: np.ndarray, n_paths: int, horizon: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """(n_paths, horizon) daily returns built from circular blocks of the sample."""
    n = len(r)
    block = max(1, min(block, n))
    n_blocks = math.ceil(horizon / block)
    starts = rng.integers(0, n, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return r[idx.reshape(n_paths, n_blocks * block)[:, :horizon]]


def simulate_capital(
    daily: np.ndarray,
    start: float,
    horizon_days: int,
    n_paths: int,
    block_days: int,
    seed: int,
    *,
    stop_level: float | None = None,
    ruin_usd: float = 5.0,
    levels: tuple[float, ...] = (100.0, 1000.0),
    loss_threshold: float = 0.40,
) -> McResult:
    r = np.asarray(daily, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        r = np.zeros(1)
    rng = np.random.default_rng(seed)
    paths = sample_paths(np.clip(r, -1.0, None), n_paths, horizon_days, block_days, rng)
    eq = start * np.cumprod(1.0 + paths, axis=1)
    stopped = np.zeros(n_paths, dtype=bool)
    if stop_level is not None:
        hit = eq <= stop_level
        stopped = hit.any(axis=1)
        first = np.where(stopped, hit.argmax(axis=1), horizon_days)
        cols = np.arange(horizon_days)[None, :]
        frozen = eq[np.arange(n_paths), np.minimum(first, horizon_days - 1)][:, None]
        eq = np.where(cols >= first[:, None], frozen, eq)  # the copy is switched off after the stop
    final = eq[:, -1]
    return McResult(
        start=start,
        horizon_days=horizon_days,
        paths=n_paths,
        sample_days=len(daily),
        median=float(np.median(final)),
        mean=float(np.mean(final)),
        p5=float(np.quantile(final, 0.05)),
        p95=float(np.quantile(final, 0.95)),
        p_ge={lv: float(np.mean(final >= lv)) for lv in levels},
        p_loss=float(np.mean(final < start * (1 - loss_threshold))),
        p_ruin=float(np.mean(eq.min(axis=1) < ruin_usd)),
        p_stop=float(np.mean(stopped)),
        loss_threshold=loss_threshold,
    )


def combine_daily(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Two sub-accounts of equal size, daily rebalanced: the portfolio return is the mean."""
    n = min(len(a), len(b))
    return 0.5 * (a[-n:] + b[-n:]) if n else np.zeros(0)

"""Skill-vs-luck statistics: Sortino, profit factor, deflated Sharpe, bootstrap p-values with BH correction."""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


def sharpe(r: np.ndarray) -> float:
    """Non-annualized Sharpe of periodic returns."""
    if len(r) < 2:
        return 0.0
    sd = float(np.std(r, ddof=1))
    return float(np.mean(r)) / sd if sd > 0 else 0.0


def sortino(r: np.ndarray, periods_per_year: float = 365.0, cap: float = 10.0) -> float:
    if len(r) < 2:
        return 0.0
    downside = math.sqrt(float(np.mean(np.minimum(r, 0.0) ** 2)))
    mean = float(np.mean(r))
    if downside <= 0:
        return cap if mean > 0 else 0.0
    return max(-cap, min(cap, mean / downside * math.sqrt(periods_per_year)))


def profit_factor(pnls: list[float] | np.ndarray, cap: float = 10.0) -> float:
    p = np.asarray(pnls, dtype=float)
    gain = float(p[p > 0].sum())
    loss = float(-p[p < 0].sum())
    if loss <= 0:
        return cap if gain > 0 else 0.0
    return min(cap, gain / loss)


def _moments(r: np.ndarray) -> tuple[float, float]:
    """(skewness, Pearson kurtosis) with safe fallbacks."""
    if len(r) < 4:
        return 0.0, 3.0
    m = float(np.mean(r))
    sd = float(np.std(r))
    if sd <= 0:
        return 0.0, 3.0
    z = (r - m) / sd
    return float(np.mean(z**3)), float(np.mean(z**4))


def probabilistic_sharpe(sr: float, sr_benchmark: float, n_obs: int, skew: float, kurt: float) -> float:
    """PSR: P(true Sharpe > benchmark) given sample size and non-normality (Bailey & López de Prado)."""
    if n_obs < 3:
        return 0.0
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    denom = math.sqrt(max(denom, 1e-6))
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n_obs - 1) / denom)


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """E[max Sharpe] among `n_trials` unskilled strategies with Sharpe variance `sr_variance`."""
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1.0 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(r: np.ndarray, sr_variance: float, n_trials: int) -> float:
    """DSR = PSR against the expected maximum Sharpe of `n_trials` tries (0..1, higher = more skill)."""
    if len(r) < 10:
        return 0.0
    skew, kurt = _moments(r)
    return probabilistic_sharpe(sharpe(r), expected_max_sharpe(sr_variance, n_trials), len(r), skew, kurt)


def block_bootstrap_indices(n: int, n_samples: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """Circular moving-block bootstrap: (n_samples, n) index matrix."""
    block = max(1, min(block, n))
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n, size=(n_samples, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(n_samples, n_blocks * block)[:, :n]


def bootstrap_pvalue(r: np.ndarray, n_samples: int = 2000, block: int = 5, seed: int = 0) -> float:
    """One-sided p-value for H0: mean return ≤ 0 (block bootstrap of the demeaned series)."""
    if len(r) < 10:
        return 1.0
    rng = np.random.default_rng(seed)
    centered = r - np.mean(r)
    idx = block_bootstrap_indices(len(r), n_samples, block, rng)
    means = centered[idx].mean(axis=1)
    obs = float(np.mean(r))
    return float((1 + np.sum(means >= obs)) / (n_samples + 1))


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """BH-adjusted q-values in the input order."""
    m = len(pvalues)
    if m == 0:
        return []
    order = np.argsort(pvalues)
    p = np.asarray(pvalues, dtype=float)[order]
    q = p * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(q, 1.0)
    return out.tolist()


def clip01(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, x))

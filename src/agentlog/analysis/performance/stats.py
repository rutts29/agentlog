from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class IntervalEstimate:
    estimate: float
    low: float
    high: float
    n: int


def wilson_interval(successes: int, n: int, z: float = 1.96) -> IntervalEstimate:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return IntervalEstimate(estimate=float("nan"), low=float("nan"), high=float("nan"), n=0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return IntervalEstimate(
        estimate=p,
        low=max(0.0, center - margin),
        high=min(1.0, center + margin),
        n=n,
    )


def risk_difference_wilson(
    successes_a: int,
    n_a: int,
    successes_b: int,
    n_b: int,
    z: float = 1.96,
) -> IntervalEstimate:
    """Risk difference A−B with Newcombe hybrid score-style bounds (approx)."""
    if n_a <= 0 or n_b <= 0:
        return IntervalEstimate(estimate=float("nan"), low=float("nan"), high=float("nan"), n=0)
    ia = wilson_interval(successes_a, n_a, z=z)
    ib = wilson_interval(successes_b, n_b, z=z)
    est = ia.estimate - ib.estimate
    # Newcombe method 10 approximation using Wilson margins.
    low = est - math.sqrt((ia.estimate - ia.low) ** 2 + (ib.high - ib.estimate) ** 2)
    high = est + math.sqrt((ia.high - ia.estimate) ** 2 + (ib.estimate - ib.low) ** 2)
    return IntervalEstimate(estimate=est, low=low, high=high, n=n_a + n_b)


def _median(values: list[float]) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    m = len(s) // 2
    if len(s) % 2:
        return s[m]
    return 0.5 * (s[m - 1] + s[m])


def cluster_bootstrap_median_diff(
    values_a: list[float],
    values_b: list[float],
    *,
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> IntervalEstimate:
    """Cluster bootstrap for median(A) − median(B). Each value is one root cluster."""
    if not values_a or not values_b:
        return IntervalEstimate(estimate=float("nan"), low=float("nan"), high=float("nan"), n=0)
    rng = random.Random(seed)
    point = _median(values_a) - _median(values_b)
    diffs: list[float] = []
    na, nb = len(values_a), len(values_b)
    for _ in range(n_boot):
        sa = [values_a[rng.randrange(na)] for _ in range(na)]
        sb = [values_b[rng.randrange(nb)] for _ in range(nb)]
        diffs.append(_median(sa) - _median(sb))
    diffs.sort()
    lo_i = int(math.floor((alpha / 2) * n_boot))
    hi_i = int(math.ceil((1 - alpha / 2) * n_boot)) - 1
    lo_i = max(0, min(lo_i, n_boot - 1))
    hi_i = max(0, min(hi_i, n_boot - 1))
    return IntervalEstimate(
        estimate=point,
        low=diffs[lo_i],
        high=diffs[hi_i],
        n=na + nb,
    )

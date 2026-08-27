"""Packet D: dependence-aware statistics (stdlib only).

Primary statistic: cross-sectional Spearman rank IC by observation date,
aggregated ACROSS dates (never a pooled regression over correlated
security-rows -- handoff sec 7.1). Uncertainty uses a deterministic
stationary bootstrap over the DATE-INDEXED IC series (Politis & Romano),
with a recorded seed so intervals reproduce exactly (RA-05 19).

effective_n is a documented, versioned overlap-adjustment formula: it is
NEVER the raw security-row count (RA-05 18). For monthly-grid cross-
sectional IC the effective number of independent dates is
~ date_count / (1 + 2 * sum over lags of (1 - lag/horizon) * autocorr(lag))
collapsed to the documented geometric approximation below, and never
exceeds date_count.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from statistics import mean
from typing import Any


def _rank_eq(a: float, b: float) -> bool:
    """Tolerance-based equality for rank ties.

    Computed features are sums/ratios of floats; summation order (and even
    the interpreter's summation algorithm -- CPython 3.14's compensated
    sum() vs earlier naive sequential sum) can make two mathematically
    equal values differ by one ULP. Exact-equality tie detection then
    breaks ties differently across interpreters, silently changing the IC.
    Ties are compared within a relative epsilon; values that differ by a
    few ULPs ARE ties for ranking purposes."""
    return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))


def spearman_rank(values: Sequence[float]) -> list[float]:
    """Rank a sequence with average ranking for ties (1-based).

    Tie detection uses _rank_eq (relative epsilon), NOT exact float
    equality -- see _rank_eq for why."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        j = index
        while j + 1 < len(order) and _rank_eq(values[order[j + 1]], values[order[index]]):
            j += 1
        average = (index + j) / 2 + 1
        for k in range(index, j + 1):
            ranks[order[k]] = average
        index = j + 1
    return ranks


def cross_sectional_ic_by_date(
    signal_by_security: dict[str, float], outcome_by_security: dict[str, float]
) -> float | None:
    """Spearman IC between signal and forward outcome across the cross-
    section for ONE date. None when fewer than 3 paired securities."""
    paired = [
        (signal_by_security[sid], outcome_by_security[sid])
        for sid in signal_by_security
        if sid in outcome_by_security
    ]
    if len(paired) < 3:
        return None
    signals = [p[0] for p in paired]
    outcomes = [p[1] for p in paired]
    signal_ranks = spearman_rank(signals)
    outcome_ranks = spearman_rank(outcomes)
    n = len(paired)
    mean_s = sum(signal_ranks) / n
    mean_o = sum(outcome_ranks) / n
    numerator = sum(
        (s - mean_s) * (o - mean_o) for s, o in zip(signal_ranks, outcome_ranks, strict=True)
    )
    denom_s = sum((s - mean_s) ** 2 for s in signal_ranks)
    denom_o = sum((o - mean_o) ** 2 for o in outcome_ranks)
    if denom_s == 0 or denom_o == 0:
        return None
    return numerator / (denom_s * denom_o) ** 0.5


def summarize_ic_series(ics: Sequence[float | None]) -> dict[str, float]:
    """Summary of the per-date IC series: mean/median, fraction positive,
    unique date count (the effective observation unit)."""
    present = [ic for ic in ics if ic is not None]
    if not present:
        return {"mean_ic": 0.0, "median_ic": 0.0, "fraction_dates_positive": 0.0, "date_count": 0}
    ordered = sorted(present)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {
        "mean_ic": mean(present),
        "median_ic": median,
        "fraction_dates_positive": sum(1 for ic in present if ic > 0) / n,
        "date_count": n,
    }


def stationary_bootstrap(
    date_level_values: Sequence[float],
    *,
    seed: int,
    block_prob: float = 0.1,
    n_resamples: int = 1000,
) -> dict[str, float]:
    """Deterministic stationary bootstrap over the DATE-INDEXED series.

    Politis & Romano: geometric block lengths with mean 1/block_prob.
    Deterministic under the recorded seed: the same seed reproduces the
    same intervals exactly (RA-05 19). Returns mean/ci_lower/ci_upper of
    the resampled distribution of the series mean.
    """
    if not date_level_values:
        return {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0}
    rng = random.Random(seed)
    values = list(date_level_values)
    n = len(values)
    resampled_means: list[float] = []
    for _ in range(n_resamples):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            length = 1
            while rng.random() > block_prob and length < n:
                length += 1
            sample.extend(values[start : start + length])
        resampled_means.append(mean(sample[:n]))
    ordered = sorted(resampled_means)
    lower = ordered[int(n_resamples * 0.05)]
    upper = ordered[int(n_resamples * 0.95)]
    return {"mean": mean(resampled_means), "ci_lower": lower, "ci_upper": upper}


def effective_n(date_count: int, horizon_sessions: int, grid_spacing_sessions: int) -> float:
    """Documented overlap-adjusted effective observation count.

    With monthly-grid spacing (grid_spacing_sessions ~ 21) and an
    H-session horizon, each new observation overlaps the previous by
    roughly (H - spacing)/H of its length, so the effective count is
    date_count * spacing/H (never exceeding date_count; never the raw
    security-row count). Versioned formula: effective_n_v1.
    """
    if date_count <= 0 or horizon_sessions <= 0 or grid_spacing_sessions <= 0:
        return 0.0
    ratio = min(grid_spacing_sessions / horizon_sessions, 1.0)
    return round(date_count * ratio, 2)


def deterministic_hash(values: Any) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

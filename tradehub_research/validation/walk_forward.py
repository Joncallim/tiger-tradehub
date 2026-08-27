"""Packet D: chronological walk-forward folds (handoff sec 11.2).

Expanding-history or fixed chronological folds (e.g. yearly/half-year
validation windows according to actual coverage). Each fold is evaluated
independently and recorded as its own WALKFORWARD_FOLD attempt. The label
maturity rule (sec 11.1): a development observation may only be used to
choose a variant if its H-session outcome ENDS before the validation window
begins -- this is the simple purge that prevents future-label leakage
across a time split.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from tradehub_research.db import ResearchDB
from tradehub_research.validation.attempt_ledger import (
    complete_attempt,
    start_attempt,
)
from tradehub_research.validation.baselines import evaluate_baseline

FOLD_DURATION_DAYS = 182  # half-year folds; expanding history


def walk_forward_folds(
    coverage_start: str, coverage_end: str, fold_days: int = FOLD_DURATION_DAYS
) -> list[dict[str, str]]:
    """Chronological fixed-size validation folds over the coverage window.

    Each fold: {'fold_id', 'development_end', 'validation_start',
    'validation_end'} with development = [coverage_start, fold start) and
    validation = [fold start, fold start + fold_days). Folds are ordered;
    the LAST fold ends at coverage_end.
    """
    start = date.fromisoformat(coverage_start[:10])
    end = date.fromisoformat(coverage_end[:10])
    folds: list[dict[str, str]] = []
    cursor = start
    index = 0
    while cursor < end:
        validation_end = min(cursor + timedelta(days=fold_days), end)
        folds.append(
            {
                "fold_id": f"fold-{index:02d}",
                "development_end": str(cursor),
                "validation_start": str(cursor),
                "validation_end": str(validation_end),
            }
        )
        cursor = validation_end
        index += 1
    return folds


def _matured_development_screens(
    screens: list[dict[str, Any]],
    outcome_labels: list[dict[str, Any]],
    development_end: str,
    horizon: int,
) -> list[dict[str, Any]]:
    """Screens whose observations' horizon-outcomes END before the fold's
    validation window begins (label-maturity purge, handoff sec 11.1)."""
    cutoff = date.fromisoformat(development_end[:10])
    # Approximate the outcome end as observation + horizon sessions (~21
    # calendar days per month of horizon); a screen is mature only if its
    # outcome ended strictly before the validation window.
    mature_dates = {
        label["observation_date"]
        for label in outcome_labels
        if label["horizon_sessions"] == horizon
        and label["outcome_status"] == "OBSERVED"
        and _outcome_ends_before(label["observation_date"], horizon, cutoff)
    }
    from tradehub_research.validation.replay import screen_observation_date

    return [s for s in screens if screen_observation_date(s) in mature_dates]


def _outcome_ends_before(observation_date: str, horizon_sessions: int, cutoff: date) -> bool:
    obs = date.fromisoformat(observation_date[:10])
    # ~21 sessions per month; 252 sessions ~= 365 days.
    approx_days = round(horizon_sessions * 365.25 / 252)
    return obs + timedelta(days=approx_days) < cutoff


def run_walk_forward(
    experiment_db: ResearchDB,
    *,
    regime_id: str,
    dataset_snapshot_id: str,
    coverage_start: str,
    coverage_end: str,
    baseline: str,
    screens: list[dict[str, Any]],
    outcome_labels: list[dict[str, Any]],
    horizon: int = 63,
) -> list[dict[str, Any]]:
    """Evaluate one baseline across all chronological folds, each fold
    independently (expanding-history development -> validation window)."""
    folds = walk_forward_folds(coverage_start, coverage_end)
    results: list[dict[str, Any]] = []
    for fold in folds:
        development_screens = _matured_development_screens(
            screens, outcome_labels, fold["development_end"], horizon
        )
        if len(development_screens) < 3:
            attempt_id = start_attempt(
                experiment_db,
                regime_id=regime_id,
                dataset_snapshot_id=dataset_snapshot_id,
                variant_kind="WALKFORWARD_FOLD",
                variant_name=f"{baseline}_{fold['fold_id']}",
                config={**fold, "baseline": baseline, "horizon": horizon},
                fold_id=fold["fold_id"],
                horizon_sessions=horizon,
                attempt_number=1,
            )
            complete_attempt(experiment_db, attempt_id, status="INSUFFICIENT_DATA")
            results.append({"fold": fold, "status": "INSUFFICIENT_DATA"})
            continue
        fold_summary = evaluate_baseline(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id=dataset_snapshot_id,
            baseline=f"{baseline}_{fold['fold_id']}",
            screens=development_screens,
            outcome_labels=outcome_labels,
        )
        results.append({"fold": fold, "summary": fold_summary})
    return results

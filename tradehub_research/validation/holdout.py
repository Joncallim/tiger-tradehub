"""Packet D: the sealed holdout run (handoff sec 11.3 / RA-05 17).

After the walk-forward development evaluation, the regime is SEALED
(one-time transition; dates never change) and then a SINGLE final HOLDOUT
attempt may run. The schema's seal-guard trigger enforces both sides:
- no evaluation_regime mutation after sealing (dates/spec immutable),
- no non-HOLDOUT experiment_attempt insert after sealing.

VARIANT IDENTITY (P1 fix): the holdout evaluates the EXACT frozen variant
named before sealing by calling the SAME canonical implementation used
during development evaluation -- baselines.signals_for_baseline /
portfolio_series_for_baseline and compute_ic_horizon /
compute_portfolio_horizon. There is NO separate holdout signal builder:
a variant identifier can never describe one strategy while executing
another (B0 holdout = actual B0, ..., B4 holdout = actual B4).

Because the regime is sealed BEFORE the evaluation runs, the attempt
itself MUST be recorded as variant_kind HOLDOUT (the seal guard blocks
everything else); metrics are recorded via the same _record_horizon_metric
shape as evaluate_baseline, with the attempt-kind/name the only
difference from a development evaluation.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.db import ResearchDB
from tradehub_research.validation.attempt_ledger import complete_attempt, start_attempt
from tradehub_research.validation.baselines import (
    ALL_BASELINES,
    HORIZON_SESSIONS,
    IC_BASELINES,
    PORTFOLIO_BASELINES,
    _outcome_map,
    compute_ic_horizon,
    compute_portfolio_horizon,
    group_screens_by_date_security,
    portfolio_series_for_baseline,
    record_horizon_metric,
    signals_for_baseline,
)
from tradehub_research.validation.regime import load_evaluation_regime, seal_evaluation_regime
from tradehub_research.validation.replay import screen_observation_date


def run_sealed_holdout(
    experiment_db: ResearchDB,
    *,
    regime_id: str,
    dataset_snapshot_id: str,
    baseline: str,
    screens: list[dict[str, Any]],
    outcome_labels: list[dict[str, Any]],
    bootstrap_seed: int = 20260827,
) -> dict[str, Any]:
    """Seal the regime (if unsealed) and run the single final HOLDOUT
    evaluation of ONE baseline on the holdout window.

    The holdout window is read from the regime's immutable spec -- never
    recomputed here, never influenced by performance (RA-05 17). The
    baseline signal/series and the horizon computations are the CANONICAL
    implementations shared with development evaluation (variant identity).
    """
    if baseline not in ALL_BASELINES:
        raise ValueError(f"unknown baseline variant: {baseline}")

    regime = load_evaluation_regime(experiment_db, regime_id)
    if regime["sealed_at"] is None:
        seal_evaluation_regime(experiment_db, regime_id)
        regime = load_evaluation_regime(experiment_db, regime_id)

    # The sealed HOLDOUT is a ONE-TIME evaluation: refuse to re-run once a
    # HOLDOUT attempt already exists for this regime (the seal guard allows
    # further HOLDOUT inserts, so this application-level guard is required
    # to make "single final holdout" structural -- RA-05 17).
    with experiment_db.connect(read_only=True) as conn:
        existing = conn.execute(
            "SELECT 1 FROM experiment_attempt WHERE regime_id=? AND variant_kind='HOLDOUT'",
            (regime_id,),
        ).fetchone()
    if existing is not None:
        raise ValueError(
            f"regime {regime_id} already has a sealed HOLDOUT attempt; "
            "the final holdout is a one-time evaluation"
        )

    spec = regime["spec"]
    holdout = spec["holdout_window"]
    holdout_start = holdout["start"]
    holdout_end = holdout["end"]

    holdout_screens = [
        s for s in screens if holdout_start <= screen_observation_date(s) <= holdout_end
    ]
    holdout_labels = [
        label
        for label in outcome_labels
        if holdout_start <= label["observation_date"][:10] <= holdout_end
    ]

    attempt_id = start_attempt(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id=dataset_snapshot_id,
        variant_kind="HOLDOUT",
        variant_name=f"HOLDOUT_{baseline}",
        config={
            "baseline": baseline,
            "holdout_window": holdout,
            "evaluation_mode": "IC" if baseline in IC_BASELINES else "portfolio_mean",
        },
        attempt_number=1,
    )

    if len(holdout_screens) < 3:
        complete_attempt(experiment_db, attempt_id, status="INSUFFICIENT_DATA")
        return {"status": "INSUFFICIENT_DATA", "attempt_id": attempt_id}

    try:
        grouped = group_screens_by_date_security(holdout_screens)
        summary: dict[str, Any] = {
            "baseline": baseline,
            "holdout_window": holdout,
            "horizons": {},
        }
        for horizon in HORIZON_SESSIONS:
            if baseline in PORTFOLIO_BASELINES:
                # CANONICAL portfolio series: B0 = pinned benchmark return,
                # B1 = equal-weight universe return (identical dates/costs).
                series = portfolio_series_for_baseline(baseline, grouped, holdout_labels, horizon)
                result = compute_portfolio_horizon(series, horizon, bootstrap_seed)
                metric_name = "mean_return"
                point_estimate = result.get("mean_return", 0.0)
            else:
                # CANONICAL IC signal: B2 composite / B3 any-pass / B4
                # equal-scoring -- the identical implementation used in
                # development evaluation.
                signals = signals_for_baseline(baseline, grouped)
                outcomes = _outcome_map(holdout_labels, horizon)
                result = compute_ic_horizon(signals, outcomes, horizon, bootstrap_seed)
                metric_name = "mean_ic"
                point_estimate = result.get("mean_ic", 0.0)
            record_horizon_metric(
                experiment_db,
                attempt_id,
                horizon,
                metric_name,
                point_estimate,
                result,
            )
            summary["horizons"][str(horizon)] = result
        complete_attempt(experiment_db, attempt_id)
        summary["attempt_id"] = attempt_id
        summary["status"] = "COMPLETE"
        return summary
    except Exception:
        complete_attempt(experiment_db, attempt_id, status="FAILED")
        raise

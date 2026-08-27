"""Packet D: the sealed holdout run (handoff sec 11.3 / RA-05 17).

After the walk-forward development evaluation, the regime is SEALED
(one-time transition; dates never change) and then a SINGLE final HOLDOUT
attempt may run. The schema's seal-guard trigger enforces both sides:
- no evaluation_regime mutation after sealing (dates/spec immutable),
- no non-HOLDOUT experiment_attempt insert after sealing.

The holdout variant is evaluated on the holdout window defined at regime
draft time (from dates only -- never from performance). Because the regime
is sealed BEFORE the evaluation runs, the attempt itself MUST be recorded
as variant_kind HOLDOUT (the seal guard blocks everything else); its
metrics are computed here directly, mirroring evaluate_baseline's IC
pipeline but never inserting a BASELINE-kind attempt.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.db import ResearchDB
from tradehub_research.validation.attempt_ledger import (
    complete_attempt,
    record_metric,
    start_attempt,
)
from tradehub_research.validation.regime import load_evaluation_regime, seal_evaluation_regime
from tradehub_research.validation.statistics import (
    cross_sectional_ic_by_date,
    effective_n,
    stationary_bootstrap,
    summarize_ic_series,
)

HORIZON_SESSIONS = (21, 63, 126, 252)


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
    evaluation of one baseline on the holdout window.

    The holdout window is read from the regime's immutable spec -- never
    recomputed here, never influenced by performance (RA-05 17).
    """
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

    from tradehub_research.validation.replay import screen_observation_date

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
        config={"baseline": baseline, "holdout_window": holdout},
        attempt_number=1,
    )

    if len(holdout_screens) < 3:
        complete_attempt(experiment_db, attempt_id, status="INSUFFICIENT_DATA")
        return {"status": "INSUFFICIENT_DATA", "attempt_id": attempt_id}

    try:
        by_security: dict[str, list[dict[str, Any]]] = {}
        for screen in holdout_screens:
            by_security.setdefault(screen["security_id"], []).append(screen)

        outcomes_by_horizon: dict[int, dict[str, dict[str, float]]] = {}
        for label in holdout_labels:
            if label["outcome_status"] != "OBSERVED":
                continue
            if label["benchmark_relative_return"] is None:
                continue
            horizon = label["horizon_sessions"]
            outcomes_by_horizon.setdefault(horizon, {}).setdefault(label["security_id"], {})[
                label["observation_date"]
            ] = float(label["benchmark_relative_return"])

        summary: dict[str, Any] = {"baseline": baseline, "holdout": holdout, "horizons": {}}
        for horizon in HORIZON_SESSIONS:
            outcomes = outcomes_by_horizon.get(horizon, {})
            per_date_ics: list[float | None] = []
            date_count = 0
            security_count = 0
            for day, signals in sorted(_signals_by_date(by_security).items()):
                date_outcomes = {
                    sid: outcomes[sid][day] for sid in signals if day in outcomes.get(sid, {})
                }
                ic = cross_sectional_ic_by_date(signals, date_outcomes)
                per_date_ics.append(ic)
                if ic is not None:
                    date_count += 1
                    security_count = max(security_count, len(date_outcomes))
            summarized = summarize_ic_series(per_date_ics)
            if date_count < 3:
                record_metric(
                    experiment_db,
                    attempt_id=attempt_id,
                    horizon_sessions=horizon,
                    segment="ALL",
                    metric_name="mean_ic",
                    point_estimate=0.0,
                    date_count=date_count,
                    security_count=security_count,
                    effective_n=0.0,
                    low_confidence=True,
                )
                summary["horizons"][str(horizon)] = {"verdict": "INSUFFICIENT_DATA"}
                continue
            bootstrap = stationary_bootstrap(
                [ic for ic in per_date_ics if ic is not None],
                seed=bootstrap_seed,
            )
            eff_n = effective_n(date_count, horizon, 21)
            record_metric(
                experiment_db,
                attempt_id=attempt_id,
                horizon_sessions=horizon,
                segment="ALL",
                metric_name="mean_ic",
                point_estimate=summarized["mean_ic"],
                ci_lower=bootstrap["ci_lower"],
                ci_upper=bootstrap["ci_upper"],
                bootstrap_seed=bootstrap_seed,
                bootstrap_method="stationary",
                date_count=date_count,
                security_count=security_count,
                effective_n=eff_n,
                low_confidence=date_count < 6,
            )
            summary["horizons"][str(horizon)] = {
                **summarized,
                "ci_lower": bootstrap["ci_lower"],
                "ci_upper": bootstrap["ci_upper"],
                "effective_n": eff_n,
                "low_confidence": date_count < 6,
            }
        complete_attempt(experiment_db, attempt_id)
        summary["attempt_id"] = attempt_id
        summary["status"] = "COMPLETE"
        return summary
    except Exception:
        complete_attempt(experiment_db, attempt_id, status="FAILED")
        raise


def _signals_by_date(
    screens_by_security: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    """Per-date signals keyed by the EVALUATION date (pipeline_run as_of via
    screen_observation_date), never computed_at (run wall-clock time).

    AGGREGATION, never overwrite: a security screened by six Hunter
    families contributes the MEAN family confidence (B4-equal-scoring
    semantics) -- the same aggregation rule as the evaluation baselines, so
    the holdout signal is comparable to the development signal and no
    family silently wins (round-1 P1 class, held out here)."""
    from tradehub_research.validation.replay import screen_observation_date

    signals: dict[str, dict[str, float]] = {}
    for security_id, security_screens in screens_by_security.items():
        for screen in security_screens:
            day = screen_observation_date(screen)
            bucket = signals.setdefault(day, {}).setdefault(security_id, [])
            bucket.append(float(screen.get("confidence", 0.0) or 0.0))
    return {
        day: {security_id: sum(values) / len(values) for security_id, values in securities.items()}
        for day, securities in signals.items()
    }

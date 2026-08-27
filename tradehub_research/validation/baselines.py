"""Packet C: the five mandatory baselines (handoff sec 8).

B0 -- Broad market benchmark (pinned artifact; benchmark-relative outcomes).
B1 -- PIT eligible universe: equal-weight eligible sample/universe.
B2 -- Simple transparent factor composite: equal-weight cross-sectional
      rank of ONE predeclared, monotonic diagnostic per family
      (valuation/quality/momentum) -- the mapping is frozen before OOS.
B3 -- Hunters only: candidate quality from deterministic Hunters/funnel with
      NO committee gate.
B4 -- Simple/equal scoring vs the current production scoring (comparison
      variant, not truth).

Each baseline evaluation records an experiment_attempt + metric rows
(append-only) and returns its summary dict. Inputs are the replayed
screen_result rows and the built outcome_label rows; a baseline never
re-derives features (no train/prod skew) and never mutates research.db.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.validation.attempt_ledger import (
    complete_attempt,
    record_metric,
    start_attempt,
)
from tradehub_research.validation.statistics import (
    cross_sectional_ic_by_date,
    effective_n,
    stationary_bootstrap,
    summarize_ic_series,
)

# Predeclared monotonic diagnostic per family for B2 (frozen mapping).
B2_DIAGNOSTIC_BY_FAMILY = {
    "valuation": "confidence",
    "quality": "confidence",
    "momentum_confirmation": "confidence",
}

HORIZON_SESSIONS = (21, 63, 126, 252)


def _screen_date(screen: dict[str, Any]) -> str:
    return str(screen.get("computed_at", screen.get("run_id", "")))[:10]


def _signal_pass_fail(screen: dict[str, Any]) -> float:
    return 1.0 if screen.get("passed") else 0.0


def _signal_confidence(screen: dict[str, Any]) -> float:
    return float(screen.get("confidence", 0.0) or 0.0)


def _b2_composite(screens_by_security: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    """Equal-weight cross-sectional rank of one monotonic diagnostic per
    family (B2): for each family with a predeclared diagnostic, rank the
    diagnostic across the cross-section; average the family ranks."""
    family_diagnostics: dict[str, dict[str, float]] = {}
    for family, diagnostic in B2_DIAGNOSTIC_BY_FAMILY.items():
        per_security: dict[str, float] = {}
        for security_id, screens in screens_by_security.items():
            family_screen = next((s for s in screens if s.get("family") == family), None)
            if family_screen is not None:
                per_security[security_id] = float(family_screen.get(diagnostic, 0.0) or 0.0)
        if len(per_security) >= 3:
            family_diagnostics[family] = per_security
    if not family_diagnostics:
        return {}
    from tradehub_research.validation.statistics import spearman_rank

    ranks: dict[str, dict[str, float]] = {}
    for family, per_security in family_diagnostics.items():
        ordered_ids = sorted(per_security)
        rank_values = spearman_rank([per_security[sid] for sid in ordered_ids])
        ranks[family] = {sid: rank_values[i] for i, sid in enumerate(ordered_ids)}
    composite: dict[str, float] = {}
    for security_id in {sid for r in ranks.values() for sid in r}:
        values = [r[security_id] for r in ranks.values() if security_id in r]
        composite[security_id] = sum(values) / len(values)
    return composite


def _outcome_map(outcome_labels: list[dict[str, Any]], horizon: int) -> dict[str, dict[str, float]]:
    """{security_id: {observation_date: benchmark_relative_return}} for the
    OBSERVED labels at the given horizon."""
    result: dict[str, dict[str, float]] = {}
    for label in outcome_labels:
        if label["horizon_sessions"] != horizon:
            continue
        if label["outcome_status"] != "OBSERVED":
            continue
        if label["benchmark_relative_return"] is None:
            continue
        result.setdefault(label["security_id"], {})[label["observation_date"]] = float(
            label["benchmark_relative_return"]
        )
    return result


def evaluate_baseline(
    experiment_db: Any,
    *,
    regime_id: str,
    dataset_snapshot_id: str,
    baseline: str,
    screens: list[dict[str, Any]],
    outcome_labels: list[dict[str, Any]],
    bootstrap_seed: int = 20260827,
) -> dict[str, Any]:
    """Evaluate one baseline variant across the four horizons.

    screens: replayed screen_result rows (all passes AND fails).
    outcome_labels: outcome_label rows for the same dataset snapshot.
    Records one BASELINE attempt + one metric per horizon (mean IC with
    dependence-aware CI), append-only. Returns the summary dict.
    """
    attempt_id = start_attempt(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id=dataset_snapshot_id,
        variant_kind="BASELINE",
        variant_name=baseline,
        config={"baseline": baseline, "diagnostic_mapping": B2_DIAGNOSTIC_BY_FAMILY},
        attempt_number=1,
    )
    try:
        by_security: dict[str, list[dict[str, Any]]] = {}
        for screen in screens:
            by_security.setdefault(screen["security_id"], []).append(screen)

        signals_by_date: dict[str, dict[str, float]] = {}
        for security_id, security_screens in by_security.items():
            for screen in security_screens:
                day = _screen_date(screen)
                if baseline == "B3_HUNTERS_ONLY":
                    signal = _signal_pass_fail(screen)
                elif baseline == "B4_EQUAL_SCORING":
                    signal = _signal_confidence(screen)
                else:
                    signal = _signal_confidence(screen)
                signals_by_date.setdefault(day, {})[security_id] = signal
        if baseline == "B2_FACTOR_COMPOSITE":
            composite = _b2_composite(by_security)
            for day in signals_by_date:
                signals_by_date[day] = {
                    sid: composite.get(sid, 0.0) for sid in signals_by_date[day]
                }

        summary: dict[str, Any] = {"baseline": baseline, "horizons": {}}
        for horizon in HORIZON_SESSIONS:
            outcomes = _outcome_map(outcome_labels, horizon)
            per_date_ics: list[float | None] = []
            date_count = 0
            security_count = 0
            for day, signals in sorted(signals_by_date.items()):
                if day not in {d for o in outcomes.values() for d in o}:
                    continue
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
        return summary
    except Exception:
        complete_attempt(experiment_db, attempt_id, status="FAILED")
        raise

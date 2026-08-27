"""Packet C: the five mandatory baselines (handoff sec 8).

B0 -- Broad market benchmark (pinned artifact; benchmark-relative outcomes).
      Portfolio-style: per-date mean benchmark return of the eligible
      universe -- the market reference the other baselines are compared
      against.
B1 -- PIT eligible universe: equal-weight eligible sample/universe.
      Portfolio-style: per-date mean benchmark-relative return of the
      equal-weight universe.
B2 -- Simple transparent factor composite: equal-weight cross-sectional
      rank of ONE predeclared, monotonic diagnostic per family
      (valuation/quality/momentum) -- the mapping is frozen before OOS.
      Cross-sectional IC baseline.
B3 -- Hunters only: candidate quality from deterministic Hunters/funnel with
      NO committee gate. Signal = any-family pass. Cross-sectional IC.
B4 -- Simple/equal scoring: equal family contribution (mean family
      confidence), the comparison variant for the current production
      scoring. Cross-sectional IC.

Each baseline evaluation records an experiment_attempt + metric rows
(append-only) and returns its summary dict. Inputs are the replayed
screen_result rows (date-keyed by pipeline_run.as_of -- NEVER
computed_at) and the built outcome_label rows; a baseline never re-derives
features (no train/prod skew) and never mutates research.db.

CRITICAL aggregation rule: per (date, security) the per-family screens are
AGGREGATED, never overwritten -- a security screened by six Hunters yields
one signal computed from all six families. This is what makes the
remove-one-Hunter ablation real (removing a family must change the signal).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tradehub_research.validation.attempt_ledger import (
    complete_attempt,
    record_metric,
    start_attempt,
)
from tradehub_research.validation.replay import screen_observation_date
from tradehub_research.validation.statistics import (
    cross_sectional_ic_by_date,
    effective_n,
    spearman_rank,
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

# Baselines evaluated with cross-sectional IC vs forward outcomes.
IC_BASELINES = {"B2_FACTOR_COMPOSITE", "B3_HUNTERS_ONLY", "B4_EQUAL_SCORING"}
# Baselines evaluated with portfolio-style per-date mean returns.
PORTFOLIO_BASELINES = {"B0_BENCHMARK", "B1_UNIVERSE"}


def _group_screens_by_date_security(
    screens: list[dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """{date: {security_id: [family screens]}} -- all families per
    security retained, never overwritten."""
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for screen in screens:
        day = screen_observation_date(screen)
        grouped[day][screen["security_id"]].append(screen)
    return grouped


def _b3_any_pass(family_screens: list[dict[str, Any]]) -> float:
    """Hunters-only signal: 1.0 if ANY family passed (sufficient-data pass
    is guaranteed by the screen contract), else 0.0."""
    return 1.0 if any(s.get("passed") for s in family_screens) else 0.0


def _b4_equal_scoring(family_screens: list[dict[str, Any]]) -> float:
    """Equal family contribution: mean confidence across ALL families
    (a security with six screens contributes all six; missing families are
    simply absent from the mean, matching the scoring layer's family
    weighting)."""
    confidences = [float(s.get("confidence", 0.0) or 0.0) for s in family_screens]
    if not confidences:
        return 0.0
    return sum(confidences) / len(confidences)


def _b2_composite(family_screens: list[dict[str, Any]]) -> float:
    """Simple factor composite: equal-weight cross-sectional rank of one
    predeclared diagnostic per family (B2). Requires the full cross-section
    (computed per date in evaluate_baseline, not per security here)."""
    raise NotImplementedError("computed cross-sectionally per date")


def _b2_composite_by_date(
    grouped: dict[str, dict[str, list[dict[str, Any]]]],
) -> dict[str, dict[str, float]]:
    """Per-date equal-weight composite of family ranks (frozen B2 mapping)."""
    composite: dict[str, dict[str, float]] = {}
    for day, securities in grouped.items():
        family_diagnostics: dict[str, dict[str, float]] = {}
        for family, diagnostic in B2_DIAGNOSTIC_BY_FAMILY.items():
            per_security: dict[str, float] = {}
            for security_id, family_screens in securities.items():
                family_screen = next((s for s in family_screens if s.get("family") == family), None)
                if family_screen is not None:
                    per_security[security_id] = float(family_screen.get(diagnostic, 0.0) or 0.0)
            if len(per_security) >= 3:
                family_diagnostics[family] = per_security
        if not family_diagnostics:
            composite[day] = {}
            continue
        ranks: dict[str, dict[str, float]] = {}
        for family, per_security in family_diagnostics.items():
            ordered_ids = sorted(per_security)
            rank_values = spearman_rank([per_security[sid] for sid in ordered_ids])
            ranks[family] = {sid: rank_values[i] for i, sid in enumerate(ordered_ids)}
        day_composite: dict[str, float] = {}
        for security_id in {sid for r in ranks.values() for sid in r}:
            values = [r[security_id] for r in ranks.values() if security_id in r]
            day_composite[security_id] = sum(values) / len(values)
        composite[day] = day_composite
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


def _signals_for_baseline(
    baseline: str, grouped: dict[str, dict[str, list[dict[str, Any]]]]
) -> dict[str, dict[str, float]]:
    """Per-date per-security signals for cross-sectional baselines (B2-B4)."""
    if baseline == "B2_FACTOR_COMPOSITE":
        return _b2_composite_by_date(grouped)
    signals: dict[str, dict[str, float]] = {}
    for day, securities in grouped.items():
        day_signals: dict[str, float] = {}
        for security_id, family_screens in securities.items():
            if baseline == "B3_HUNTERS_ONLY":
                day_signals[security_id] = _b3_any_pass(family_screens)
            else:  # B4_EQUAL_SCORING and any other IC baseline
                day_signals[security_id] = _b4_equal_scoring(family_screens)
        signals[day] = day_signals
    return signals


def evaluate_baseline(
    experiment_db: Any,
    *,
    regime_id: str,
    dataset_snapshot_id: str,
    baseline: str,
    screens: list[dict[str, Any]],
    outcome_labels: list[dict[str, Any]],
    bootstrap_seed: int = 20260827,
    variant_name: str | None = None,
) -> dict[str, Any]:
    """Evaluate one baseline variant across the four horizons.

    B0/B1 are portfolio-style (per-date mean returns, bootstrapped). B2-B4
    are cross-sectional IC baselines. Records one BASELINE attempt + one
    metric per horizon (append-only). Returns the summary dict.

    ``variant_name`` overrides the recorded attempt name (used by
    ablations, which evaluate an underlying baseline under an ablation
    variant's name); it defaults to the baseline name.
    """
    if baseline not in IC_BASELINES | PORTFOLIO_BASELINES:
        raise ValueError(f"unknown baseline variant: {baseline}")

    attempt_id = start_attempt(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id=dataset_snapshot_id,
        variant_kind="BASELINE",
        variant_name=variant_name or baseline,
        config={
            "baseline": baseline,
            "diagnostic_mapping": B2_DIAGNOSTIC_BY_FAMILY,
            "evaluation_mode": "IC" if baseline in IC_BASELINES else "portfolio_mean",
        },
        attempt_number=1,
    )
    try:
        grouped = _group_screens_by_date_security(screens)
        summary: dict[str, Any] = {"baseline": baseline, "horizons": {}}

        for horizon in HORIZON_SESSIONS:
            outcomes = _outcome_map(outcome_labels, horizon)
            if baseline in PORTFOLIO_BASELINES:
                result = _evaluate_portfolio_horizon(
                    experiment_db, attempt_id, horizon, grouped, outcomes, bootstrap_seed
                )
            else:
                signals = _signals_for_baseline(baseline, grouped)
                result = _evaluate_ic_horizon(
                    experiment_db, attempt_id, horizon, signals, outcomes, bootstrap_seed
                )
            summary["horizons"][str(horizon)] = result

        complete_attempt(experiment_db, attempt_id)
        summary["attempt_id"] = attempt_id
        return summary
    except Exception:
        complete_attempt(experiment_db, attempt_id, status="FAILED")
        raise


def _evaluate_ic_horizon(
    experiment_db: Any,
    attempt_id: str,
    horizon: int,
    signals_by_date: dict[str, dict[str, float]],
    outcomes: dict[str, dict[str, float]],
    bootstrap_seed: int,
) -> dict[str, Any]:
    per_date_ics: list[float | None] = []
    date_count = 0
    security_count = 0
    for day, signals in sorted(signals_by_date.items()):
        date_outcomes = {sid: outcomes[sid][day] for sid in signals if day in outcomes.get(sid, {})}
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
        return {"verdict": "INSUFFICIENT_DATA"}
    bootstrap = stationary_bootstrap(
        [ic for ic in per_date_ics if ic is not None], seed=bootstrap_seed
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
    return {
        **summarized,
        "ci_lower": bootstrap["ci_lower"],
        "ci_upper": bootstrap["ci_upper"],
        "effective_n": eff_n,
        "low_confidence": date_count < 6,
    }


def _evaluate_portfolio_horizon(
    experiment_db: Any,
    attempt_id: str,
    horizon: int,
    grouped: dict[str, dict[str, list[dict[str, Any]]]],
    outcomes: dict[str, dict[str, float]],
    bootstrap_seed: int,
) -> dict[str, Any]:
    """B0/B1 portfolio-style evaluation: per-date mean benchmark-relative
    return of the equal-weight eligible universe (B1), or per-date mean
    benchmark return (B0 -- the market reference). Summarized with the
    stationary bootstrap over dates."""
    per_date_means: list[float | None] = []
    date_count = 0
    security_count = 0
    for day, securities in sorted(grouped.items()):
        date_returns = [outcomes[sid][day] for sid in securities if day in outcomes.get(sid, {})]
        if not date_returns:
            per_date_means.append(None)
            continue
        per_date_means.append(sum(date_returns) / len(date_returns))
        date_count += 1
        security_count = max(security_count, len(date_returns))
    present = [m for m in per_date_means if m is not None]
    if date_count < 3:
        record_metric(
            experiment_db,
            attempt_id=attempt_id,
            horizon_sessions=horizon,
            segment="ALL",
            metric_name="mean_return",
            point_estimate=0.0,
            date_count=date_count,
            security_count=security_count,
            effective_n=0.0,
            low_confidence=True,
        )
        return {"verdict": "INSUFFICIENT_DATA"}
    bootstrap = stationary_bootstrap(present, seed=bootstrap_seed)
    mean_return = sum(present) / len(present)
    eff_n = effective_n(date_count, horizon, 21)
    record_metric(
        experiment_db,
        attempt_id=attempt_id,
        horizon_sessions=horizon,
        segment="ALL",
        metric_name="mean_return",
        point_estimate=mean_return,
        ci_lower=bootstrap["ci_lower"],
        ci_upper=bootstrap["ci_upper"],
        bootstrap_seed=bootstrap_seed,
        bootstrap_method="stationary",
        date_count=date_count,
        security_count=security_count,
        effective_n=eff_n,
        low_confidence=date_count < 6,
    )
    return {
        "mean_return": mean_return,
        "ci_lower": bootstrap["ci_lower"],
        "ci_upper": bootstrap["ci_upper"],
        "date_count": date_count,
        "security_count": security_count,
        "effective_n": eff_n,
        "low_confidence": date_count < 6,
    }

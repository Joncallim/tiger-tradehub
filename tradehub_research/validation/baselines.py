"""Packet C: the five mandatory baselines (handoff sec 8).

B0 -- Broad market benchmark: the PINNED benchmark return itself, per
      evaluation date (the market reference every other baseline is
      compared against). Portfolio-style.
B1 -- PIT eligible universe: EQUAL-WEIGHT eligible/bootstrap-cohort return,
      per evaluation date (mean total_return across the universe). NOT the
      benchmark-relative series: B0 and B1 are economically distinct
      series under identical dates/cost conventions.
B2 -- Simple transparent factor composite: equal-weight cross-sectional
      rank of ONE predeclared, monotonic diagnostic per family
      (valuation/quality/momentum) -- the mapping is frozen before OOS.
      Cross-sectional IC baseline.
B3 -- Hunters only: candidate quality from deterministic Hunters/funnel with
      NO committee gate. Signal = any-family pass. Cross-sectional IC.
B4 -- Simple/equal scoring: equal family contribution (mean family
      confidence), the comparison variant for the current production
      scoring. Cross-sectional IC.

CANONICAL IMPLEMENTATION (variant identity -- P1 fix): the per-baseline
signal builders (signals_for_baseline, portfolio_series_for_baseline) and
the horizon computations (compute_ic_horizon, compute_portfolio_horizon)
are the ONE implementation used BOTH by development evaluation
(evaluate_baseline) AND by the sealed holdout (run_sealed_holdout). A
variant identifier can never describe one strategy while executing another
-- the holdout calls these same functions.

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

ALL_BASELINES = IC_BASELINES | PORTFOLIO_BASELINES


def group_screens_by_date_security(
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
    OBSERVED labels at the given horizon -- the cross-sectional outcome for
    IC baselines (B2-B4)."""
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


def _universe_return_by_date(
    outcome_labels: list[dict[str, Any]], horizon: int
) -> dict[str, float]:
    """{observation_date: equal-weight universe return} -- B1's series.

    The equal-weight eligible/bootstrap-cohort return for a date is the
    mean TOTAL return across the OBSERVED securities that date (identical
    dates/cost conventions to B0, but the universe's own return, NOT the
    benchmark's)."""
    per_date: dict[str, list[float]] = defaultdict(list)
    for label in outcome_labels:
        if label["horizon_sessions"] != horizon:
            continue
        if label["outcome_status"] != "OBSERVED":
            continue
        if label["total_return"] is None:
            continue
        per_date[label["observation_date"]].append(float(label["total_return"]))
    return {day: sum(values) / len(values) for day, values in per_date.items()}


def _benchmark_return_by_date(
    outcome_labels: list[dict[str, Any]], horizon: int
) -> dict[str, float]:
    """{observation_date: benchmark return} -- B0's series.

    The pinned benchmark return itself: for a given date/horizon the
    benchmark return is a single value across all securities (labels carry
    the same benchmark_return; the first non-None value per date wins --
    a disagreement across labels for the same date/horizon would indicate
    a benchmark-artifact corruption)."""
    per_date: dict[str, float] = {}
    for label in outcome_labels:
        if label["horizon_sessions"] != horizon:
            continue
        if label["outcome_status"] != "OBSERVED":
            continue
        if label["benchmark_return"] is None:
            continue
        day = label["observation_date"]
        value = float(label["benchmark_return"])
        if day in per_date and per_date[day] != value:
            raise ValueError(
                f"benchmark_return disagrees across labels for {day}/h{horizon}: "
                f"{per_date[day]} vs {value}; pinned benchmark artifact corrupted"
            )
        per_date.setdefault(day, value)
    return per_date


def signals_for_baseline(
    baseline: str, grouped: dict[str, dict[str, list[dict[str, Any]]]]
) -> dict[str, dict[str, float]]:
    """Per-date per-security signals for cross-sectional baselines (B2-B4).

    CANONICAL: the identical implementation used by development evaluation
    and the sealed holdout -- a variant identifier can never describe one
    strategy while executing another."""
    if baseline not in IC_BASELINES:
        raise ValueError(f"{baseline} is not an IC baseline; use portfolio_series_for_baseline")
    if baseline == "B2_FACTOR_COMPOSITE":
        return _b2_composite_by_date(grouped)
    signals: dict[str, dict[str, float]] = {}
    for day, securities in grouped.items():
        day_signals: dict[str, float] = {}
        for security_id, family_screens in securities.items():
            if baseline == "B3_HUNTERS_ONLY":
                day_signals[security_id] = _b3_any_pass(family_screens)
            else:  # B4_EQUAL_SCORING
                day_signals[security_id] = _b4_equal_scoring(family_screens)
        signals[day] = day_signals
    return signals


def portfolio_series_for_baseline(
    baseline: str,
    grouped: dict[str, dict[str, list[dict[str, Any]]]],
    outcome_labels: list[dict[str, Any]],
    horizon: int,
) -> dict[str, float]:
    """Per-date return series for portfolio baselines (B0/B1).

    B0_BENCHMARK: the pinned benchmark return itself.
    B1_UNIVERSE:   the equal-weight eligible/bootstrap-cohort return.

    Both use the SAME dates (the dates with OBSERVED labels) and the SAME
    cost conventions (none applied -- frictionless, documented); the two
    series are economically distinct. CANONICAL: identical implementation
    in development evaluation and the sealed holdout."""
    if baseline == "B0_BENCHMARK":
        return _benchmark_return_by_date(outcome_labels, horizon)
    if baseline == "B1_UNIVERSE":
        return _universe_return_by_date(outcome_labels, horizon)
    raise ValueError(f"{baseline} is not a portfolio baseline; use signals_for_baseline")


def compute_ic_horizon(
    signals_by_date: dict[str, dict[str, float]],
    outcomes: dict[str, dict[str, float]],
    horizon: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    """Pure IC computation for one horizon (no DB writes).

    CANONICAL computation used by evaluate_baseline AND run_sealed_holdout.
    Returns the summary dict; verdict INSUFFICIENT_DATA when fewer than 3
    dates produce an IC."""
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
        return {
            "verdict": "INSUFFICIENT_DATA",
            "date_count": date_count,
            "security_count": security_count,
            "effective_n": 0.0,
        }
    bootstrap = stationary_bootstrap(
        [ic for ic in per_date_ics if ic is not None], seed=bootstrap_seed
    )
    eff_n = effective_n(date_count, horizon, 21)
    return {
        **summarized,
        "ci_lower": bootstrap["ci_lower"],
        "ci_upper": bootstrap["ci_upper"],
        "effective_n": eff_n,
        "date_count": date_count,
        "security_count": security_count,
        "low_confidence": date_count < 6,
        "bootstrap_seed": bootstrap_seed,
    }


def compute_portfolio_horizon(
    series_by_date: dict[str, float],
    horizon: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    """Pure portfolio-style computation for one horizon (no DB writes).

    CANONICAL computation used by evaluate_baseline AND run_sealed_holdout
    over the baseline's OWN return series (benchmark vs universe)."""
    present = [series_by_date[day] for day in sorted(series_by_date)]
    date_count = len(present)
    if date_count < 3:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "date_count": date_count,
            "security_count": 0,
            "effective_n": 0.0,
        }
    bootstrap = stationary_bootstrap(present, seed=bootstrap_seed)
    mean_return = sum(present) / len(present)
    eff_n = effective_n(date_count, horizon, 21)
    return {
        "mean_return": mean_return,
        "ci_lower": bootstrap["ci_lower"],
        "ci_upper": bootstrap["ci_upper"],
        "date_count": date_count,
        "security_count": date_count,
        "effective_n": eff_n,
        "low_confidence": date_count < 6,
        "bootstrap_seed": bootstrap_seed,
    }


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

    B0/B1 are portfolio-style (benchmark return / equal-weight universe
    return). B2-B4 are cross-sectional IC baselines. Records one BASELINE
    attempt + one metric per horizon (append-only). Returns the summary
    dict.

    ``variant_name`` overrides the recorded attempt name (used by
    ablations, which evaluate an underlying baseline under an ablation
    variant's name); it defaults to the baseline name.
    """
    if baseline not in ALL_BASELINES:
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
        grouped = group_screens_by_date_security(screens)
        summary: dict[str, Any] = {"baseline": baseline, "horizons": {}}

        for horizon in HORIZON_SESSIONS:
            if baseline in PORTFOLIO_BASELINES:
                series = portfolio_series_for_baseline(baseline, grouped, outcome_labels, horizon)
                result = compute_portfolio_horizon(series, horizon, bootstrap_seed)
                metric_name = "mean_return"
                point_estimate = result.get("mean_return", 0.0)
            else:
                signals = signals_for_baseline(baseline, grouped)
                outcomes = _outcome_map(outcome_labels, horizon)
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
        return summary
    except Exception:
        complete_attempt(experiment_db, attempt_id, status="FAILED")
        raise


def record_horizon_metric(
    experiment_db: Any,
    attempt_id: str,
    horizon: int,
    metric_name: str,
    point_estimate: float,
    result: dict[str, Any],
) -> None:
    """Record one metric row from a compute_*_horizon result (append-only).

    Shared by evaluate_baseline and run_sealed_holdout so both record the
    same metric shape for the same computation."""
    record_metric(
        experiment_db,
        attempt_id=attempt_id,
        horizon_sessions=horizon,
        segment="ALL",
        metric_name=metric_name,
        point_estimate=point_estimate,
        ci_lower=result.get("ci_lower"),
        ci_upper=result.get("ci_upper"),
        bootstrap_seed=result.get("bootstrap_seed"),
        bootstrap_method="stationary" if "ci_lower" in result else None,
        date_count=result.get("date_count", 0),
        security_count=result.get("security_count", 0),
        effective_n=result.get("effective_n", 0.0),
        low_confidence=result.get("low_confidence", True),
    )

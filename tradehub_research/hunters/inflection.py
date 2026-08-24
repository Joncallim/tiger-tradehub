"""inflection / inflection_revenue_margin_acceleration / 1 (design section 3.2).

Pass iff the latest standalone-quarter revenue YoY, its acceleration versus the
prior YoY, and the operating-margin delta all meet their floors.  Only
source-reported standalone quarters, or YTD subtraction where both values come
from the same accession/concept/unit/dimensions/fiscal-year with compatible
durations, are accepted.  Any ambiguity yields ``noncomparable_periods``.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.hunters.common import (
    OPERATING_INCOME_CONCEPTS,
    REVENUE_CONCEPTS,
    active_facts,
    add_years,
    as_date,
    fact_ref,
    facts_for,
    feature_value,
    filing_age_ok,
    insufficient,
    is_unsupported_sector,
    min_freshness,
    negative,
    parse_ts,
    positive,
)
from tradehub_research.screens import ScreenContext, ScreenResultPayload, ScreenSpec, SecurityId

SCREEN_SPEC = ScreenSpec(
    family="inflection",
    screen_id="inflection_revenue_margin_acceleration",
    screen_version=1,
    feature_schema_version=1,
    parameters={
        "min_latest_revenue_yoy": 0.05,
        "min_acceleration": 0.05,
        "min_operating_margin_delta": 0.0,
        "period_duration_tolerance_days": 10,
        "max_filing_age_days": 150,
    },
    required_features=["revenue_quarters", "operating_income_quarters"],
    implementation_id="hunters/inflection.py:v1",
)

_QUARTER_MIN_DAYS = 70
_QUARTER_MAX_DAYS = 110


def _duration(fact: dict[str, Any]) -> int | None:
    start, end = fact.get("period_start"), fact.get("period_end")
    if not start or not end:
        return None
    return (as_date(end) - as_date(start)).days


def _same_basis(left: dict[str, Any], right: dict[str, Any], tolerance_days: int) -> bool:
    """Two facts are comparable only on identical concept/unit/dimensions and
    near-identical durations (design section 3.2)."""
    for key in ("concept", "unit", "dimensions"):
        if left.get(key) != right.get(key):
            return False
    left_duration, right_duration = _duration(left), _duration(right)
    if left_duration is None or right_duration is None:
        return False
    return abs(left_duration - right_duration) <= tolerance_days


def _standalone_quarters(
    facts: list[dict[str, Any]], tolerance_days: int
) -> dict[str, dict[str, Any]] | None:
    """Quarterly series keyed by period end; None when basis is ambiguous.

    A source-reported standalone quarter (70-110 days) wins.  Otherwise a YTD
    pair from the same accession/concept/unit/dimensions/fiscal-year may be
    subtracted.  Any remaining ambiguity yields None (noncomparable_periods).
    """
    quarters: dict[str, dict[str, Any]] = {}
    standalone = [
        f
        for f in facts
        if (d := _duration(f)) is not None and _QUARTER_MIN_DAYS <= d <= _QUARTER_MAX_DAYS
    ]
    for fact in standalone:
        end = fact["period_end"]
        existing = quarters.get(end)
        if existing is None or (fact.get("public_available_time") or "") > (
            existing.get("public_available_time") or ""
        ):
            quarters[end] = fact

    covered_starts = {f["period_start"] for f in standalone}
    ytd = [
        f for f in facts if (d := _duration(f)) is not None and d > _QUARTER_MAX_DAYS and d < 370
    ]
    by_year: dict[Any, list[dict[str, Any]]] = {}
    for fact in ytd:
        if fact.get("period_start") in covered_starts:
            continue
        by_year.setdefault(fact.get("fiscal_year"), []).append(fact)
    for year_facts in by_year.values():
        year_facts.sort(key=lambda f: _duration(f) or 0)
        for index, longer in enumerate(year_facts):
            for shorter in year_facts[:index]:
                if shorter.get("period_start") != longer.get("period_start"):
                    continue
                if shorter.get("accession") != longer.get("accession"):
                    continue
                derived_end = longer["period_end"]
                if derived_end in quarters:
                    continue
                if not _same_basis(shorter, longer, tolerance_days):
                    return None  # ambiguous basis inside a YTD subtraction
                derived = dict(longer)
                derived["value"] = float(longer["value"]) - float(shorter["value"])
                derived["period_start"] = shorter["period_end"]
                derived["period_end"] = derived_end
                derived["derived_by"] = "ytd_subtraction"
                duration = _duration(derived)
                if duration is None or not (_QUARTER_MIN_DAYS <= duration <= _QUARTER_MAX_DAYS):
                    return None
                quarters[derived_end] = derived
    return quarters


def _features(latest=None, prior=None, latest_prev=None, prior_prev=None):
    def entry(fact, role):
        return feature_value(
            None if fact is None else float(fact["value"]),
            "usd" if fact is None else (fact.get("unit") or "usd"),
            [] if fact is None else [fact_ref(fact, role)],
        )

    return {
        "latest_revenue_yoy": feature_value(None, "ratio", []),
        "prior_revenue_yoy": feature_value(None, "ratio", []),
        "revenue_acceleration": feature_value(None, "ratio", []),
        "operating_margin_delta": feature_value(None, "ratio", []),
        "latest_revenue": entry(latest, "latest_revenue"),
        "prior_revenue": entry(prior, "prior_revenue"),
        "latest_revenue_year_ago": entry(latest_prev, "latest_revenue_year_ago"),
        "prior_revenue_year_ago": entry(prior_prev, "prior_revenue_year_ago"),
    }


def evaluate(context: ScreenContext, security_id: SecurityId) -> ScreenResultPayload:
    as_of = parse_ts(context.as_of)
    params = SCREEN_SPEC.parameters
    max_age = int(params["max_filing_age_days"])
    tolerance = int(params["period_duration_tolerance_days"])

    if is_unsupported_sector(context, security_id):
        return insufficient(["unsupported_sector"], _features(), 0.0)

    facts = active_facts(context, security_id)
    revenue_facts = facts_for(facts, REVENUE_CONCEPTS)
    income_facts = facts_for(facts, OPERATING_INCOME_CONCEPTS)
    revenue_quarters = _standalone_quarters(revenue_facts, tolerance)
    income_quarters = _standalone_quarters(income_facts, tolerance)
    if revenue_quarters is None or income_quarters is None:
        return insufficient(["noncomparable_periods"], _features(), 0.0)

    fresh_revenue = {
        end: fact for end, fact in revenue_quarters.items() if filing_age_ok(fact, as_of, max_age)
    }
    ends = sorted(fresh_revenue, reverse=True)
    if len(ends) < 2:
        return insufficient(["missing_quarterly_revenue"], _features(), 0.0)

    latest_end, prior_end = ends[0], ends[1]
    latest = fresh_revenue[latest_end]
    prior = fresh_revenue[prior_end]

    def year_ago(series: dict[str, dict[str, Any]], end: str) -> dict[str, Any] | None:
        target = add_years(as_date(end), -1)
        candidates = [
            fact
            for fact_end, fact in series.items()
            if abs((as_date(fact_end) - target).days) <= tolerance
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    latest_prev = year_ago(revenue_quarters, latest_end)
    prior_prev = year_ago(revenue_quarters, prior_end)
    latest_income = income_quarters.get(latest_end)
    latest_income_prev = year_ago(income_quarters, latest_end)

    features = _features(latest, prior, latest_prev, prior_prev)

    if latest_prev is None or prior_prev is None:
        return insufficient(["missing_year_ago_comparator"], features, 0.0)
    if not _same_basis(latest, latest_prev, tolerance) or not _same_basis(
        prior, prior_prev, tolerance
    ):
        return insufficient(["noncomparable_periods"], features, 0.0)
    if latest_income is None or latest_income_prev is None:
        return insufficient(["missing_operating_income"], features, 0.0)
    if not filing_age_ok(latest_income, as_of, max_age) or not filing_age_ok(
        latest_income_prev, as_of, max_age
    ):
        return insufficient(["stale_operating_income"], features, 0.0)
    if not _same_basis(latest_income, latest_income_prev, tolerance):
        return insufficient(["noncomparable_periods"], features, 0.0)

    latest_rev, prior_rev = float(latest["value"]), float(prior["value"])
    latest_prev_rev, prior_prev_rev = float(latest_prev["value"]), float(prior_prev["value"])
    if latest_prev_rev <= 0 or prior_prev_rev <= 0:
        return negative(["nonpositive_base_revenue"], features, 1.0, 0.5)

    latest_yoy = latest_rev / latest_prev_rev - 1.0
    prior_yoy = prior_rev / prior_prev_rev - 1.0
    acceleration = latest_yoy - prior_yoy
    margin_now = float(latest_income["value"]) / latest_rev if latest_rev > 0 else None
    margin_prev = (
        float(latest_income_prev["value"]) / latest_prev_rev if latest_prev_rev > 0 else None
    )
    if margin_now is None or margin_prev is None:
        return negative(["nonpositive_base_revenue"], features, 1.0, 0.5)
    margin_delta = margin_now - margin_prev

    features["latest_revenue_yoy"] = feature_value(
        round(latest_yoy, 6),
        "ratio",
        [fact_ref(latest, "latest"), fact_ref(latest_prev, "year_ago")],
    )
    features["prior_revenue_yoy"] = feature_value(
        round(prior_yoy, 6), "ratio", [fact_ref(prior, "prior"), fact_ref(prior_prev, "year_ago")]
    )
    features["revenue_acceleration"] = feature_value(round(acceleration, 6), "ratio", [])
    features["operating_margin_delta"] = feature_value(
        round(margin_delta, 6),
        "ratio",
        [fact_ref(latest_income, "latest_income"), fact_ref(latest_income_prev, "year_ago")],
    )

    pats = [
        fact.get("public_available_time")
        for fact in (latest, prior, latest_prev, prior_prev, latest_income, latest_income_prev)
    ]
    quality = min_freshness(pats, as_of, max_age)

    passed = (
        latest_yoy >= float(params["min_latest_revenue_yoy"])
        and acceleration >= float(params["min_acceleration"])
        and margin_delta >= float(params["min_operating_margin_delta"])
    )
    if passed:
        return positive(["inflection_floors_met"], features, 1.0, quality)
    return negative(["inflection_floors_not_met"], features, 1.0, quality)

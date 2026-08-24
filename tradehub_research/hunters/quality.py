"""quality / quality_cash_return_consistency / 1 (design section 3.3).

Pass iff ROA TTM, cash conversion TTM, and the count of positive-OCF fiscal
years all meet their floors.  Cash conversion is formed only with positive net
income; nonpositive net income is a sufficient negative.  Banks, insurers and
REITs are ``unsupported_sector`` insufficient for quality v1.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.hunters.common import (
    ASSETS_CONCEPTS,
    NET_INCOME_CONCEPTS,
    OCF_CONCEPTS,
    REIT_UNSUPPORTED,
    active_facts,
    add_years,
    as_date,
    fact_ref,
    facts_for,
    feature_value,
    filing_age_ok,
    fiscal_year_ocf,
    insufficient,
    is_unsupported_sector,
    min_freshness,
    negative,
    parse_ts,
    pick_ttm,
    positive,
)
from tradehub_research.screens import ScreenContext, ScreenResultPayload, ScreenSpec, SecurityId

SCREEN_SPEC = ScreenSpec(
    family="quality",
    screen_id="quality_cash_return_consistency",
    screen_version=1,
    feature_schema_version=1,
    parameters={
        "min_roa": 0.05,
        "min_cash_conversion": 0.8,
        "min_positive_ocf_years": 3,
        "max_filing_age_days": 150,
    },
    required_features=[
        "net_income_ttm",
        "ocf_ttm",
        "assets_begin",
        "assets_end",
        "ocf_fiscal_years",
    ],
    implementation_id="hunters/quality.py:v1",
)


def _instant_at_or_before(
    facts: list[dict[str, Any]], target, tolerance_days: int
) -> dict[str, Any] | None:
    candidates = [
        fact
        for fact in facts
        if fact.get("period_end")
        and abs((as_date(fact["period_end"]) - target).days) <= tolerance_days
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f["period_end"], f.get("public_available_time") or ""))
    return candidates[-1]


def _features(
    net_income=None,
    ocf=None,
    assets_begin=None,
    assets_end=None,
    ocf_years: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    year_sources = [fact_ref(f, f"ocf_fy_{f.get('period_end')}") for f in (ocf_years or [])]
    return {
        "net_income_ttm": feature_value(
            None if net_income is None else float(net_income["value"]),
            "usd",
            [] if net_income is None else [fact_ref(net_income, "net_income_ttm")],
        ),
        "ocf_ttm": feature_value(
            None if ocf is None else float(ocf["value"]),
            "usd",
            [] if ocf is None else [fact_ref(ocf, "ocf_ttm")],
        ),
        "assets_begin": feature_value(
            None if assets_begin is None else float(assets_begin["value"]),
            "usd",
            [] if assets_begin is None else [fact_ref(assets_begin, "assets_begin")],
        ),
        "assets_end": feature_value(
            None if assets_end is None else float(assets_end["value"]),
            "usd",
            [] if assets_end is None else [fact_ref(assets_end, "assets_end")],
        ),
        "ocf_fiscal_years": feature_value(
            None if ocf_years is None else len(ocf_years), "years", year_sources
        ),
        "positive_ocf_years": feature_value(None, "years", []),
        "roa_ttm": feature_value(None, "ratio", []),
        "cash_conversion_ttm": feature_value(None, "ratio", []),
    }


def evaluate(context: ScreenContext, security_id: SecurityId) -> ScreenResultPayload:
    as_of = parse_ts(context.as_of)
    params = SCREEN_SPEC.parameters
    max_age = int(params["max_filing_age_days"])

    if is_unsupported_sector(context, security_id, extra=REIT_UNSUPPORTED):
        return insufficient(["unsupported_sector"], _features(), 0.0)

    facts = active_facts(context, security_id)
    net_income = pick_ttm(facts_for(facts, NET_INCOME_CONCEPTS), as_of, max_age)
    ocf = pick_ttm(facts_for(facts, OCF_CONCEPTS), as_of, max_age)
    asset_facts = [
        fact for fact in facts_for(facts, ASSETS_CONCEPTS) if filing_age_ok(fact, as_of, max_age)
    ]
    ocf_years = [
        fact
        for fact in fiscal_year_ocf(facts_for(facts, OCF_CONCEPTS), as_of)
        if filing_age_ok(fact, as_of, max_age)
    ]

    assets_end = None
    assets_begin = None
    if net_income is not None and net_income.get("period_end"):
        end_day = as_date(net_income["period_end"])
        assets_end = _instant_at_or_before(asset_facts, end_day, 45)
        assets_begin = _instant_at_or_before(asset_facts, add_years(end_day, -1), 45)

    features = _features(net_income, ocf, assets_begin, assets_end, ocf_years or None)

    missing = []
    if net_income is None or ocf is None:
        missing.append("missing_financial_facts")
    if net_income is not None and (assets_begin is None or assets_end is None):
        missing.append("missing_balance_sheet")
    if len(ocf_years) < int(params["min_positive_ocf_years"]):
        missing.append("insufficient_fiscal_year_history")
    if missing:
        return insufficient(missing, features, 0.0)

    ni = float(net_income["value"])
    ocf_value = float(ocf["value"])
    average_assets = (float(assets_begin["value"]) + float(assets_end["value"])) / 2.0

    positive_years = sum(1 for fact in ocf_years if float(fact["value"]) > 0)
    features["positive_ocf_years"] = feature_value(
        positive_years, "years", features["ocf_fiscal_years"]["sources"]
    )

    if ni <= 0:
        # Nonpositive net income is a sufficient negative (design section 3.3).
        features["roa_ttm"] = feature_value(None, "ratio", [])
        features["cash_conversion_ttm"] = feature_value(None, "ratio", [])
        return negative(["nonpositive_net_income"], features, 1.0, 0.5)
    if average_assets <= 0:
        return insufficient(["noncomparable_assets"], features, 0.0)

    roa = ni / average_assets
    cash_conversion = ocf_value / ni  # formed only with positive net income
    features["roa_ttm"] = feature_value(
        round(roa, 6), "ratio", [fact_ref(net_income, "net_income_ttm")]
    )
    features["cash_conversion_ttm"] = feature_value(
        round(cash_conversion, 6),
        "ratio",
        [fact_ref(ocf, "ocf_ttm"), fact_ref(net_income, "net_income_ttm")],
    )

    pats = [
        fact.get("public_available_time")
        for fact in (net_income, ocf, assets_begin, assets_end, *ocf_years[:3])
    ]
    quality = min_freshness(pats, as_of, max_age)

    passed = (
        roa >= float(params["min_roa"])
        and cash_conversion >= float(params["min_cash_conversion"])
        and positive_years >= int(params["min_positive_ocf_years"])
    )
    if passed:
        return positive(["quality_floors_met"], features, 1.0, quality)
    return negative(["quality_floors_not_met"], features, 1.0, quality)

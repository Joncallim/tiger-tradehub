"""valuation / valuation_earnings_fcf_yield / 1 (design section 3.1).

Pass iff TTM earnings yield and TTM free-cash-flow yield both meet their floors
against a PIT market cap (close x latest eligible shares fact, split-adjusted by
eligible actions only).  Nonpositive earnings/FCF is a sufficient negative;
absent or noncomparable capex, shares, or market cap is insufficient.  Banks and
insurers are ``unsupported_sector`` insufficient.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.hunters.common import (
    CAPEX_CONCEPTS,
    NET_INCOME_CONCEPTS,
    OCF_CONCEPTS,
    active_facts,
    bar_ref,
    fact_ref,
    facts_for,
    feature_value,
    insufficient,
    is_unsupported_sector,
    latest_market_cap,
    min_freshness,
    negative,
    parse_ts,
    pick_ttm,
    positive,
)
from tradehub_research.screens import ScreenContext, ScreenResultPayload, ScreenSpec, SecurityId

SCREEN_SPEC = ScreenSpec(
    family="valuation",
    screen_id="valuation_earnings_fcf_yield",
    screen_version=1,
    feature_schema_version=1,
    parameters={
        "min_earnings_yield": 0.05,
        "min_fcf_yield": 0.03,
        "require_positive_both": True,
        "max_filing_age_days": 150,
        "max_price_bar_age_trading_days": 5,
    },
    required_features=["net_income_ttm", "ocf_ttm", "capex_ttm", "market_cap"],
    implementation_id="hunters/valuation.py:v1",
)


def _features(
    net_income: dict[str, Any] | None = None,
    ocf: dict[str, Any] | None = None,
    capex: dict[str, Any] | None = None,
    bar: dict[str, Any] | None = None,
    shares_fact: dict[str, Any] | None = None,
    market_cap: float | None = None,
) -> dict[str, Any]:
    mcap_sources = []
    if bar is not None:
        mcap_sources.append(bar_ref(bar, "close"))
    if shares_fact is not None:
        mcap_sources.append(fact_ref(shares_fact, "shares"))
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
        "capex_ttm": feature_value(
            None if capex is None else float(capex["value"]),
            "usd",
            [] if capex is None else [fact_ref(capex, "capex_ttm")],
        ),
        "earnings_yield_ttm": feature_value(None, "ratio", []),
        "fcf_yield_ttm": feature_value(None, "ratio", []),
        "market_cap": feature_value(market_cap, "usd", mcap_sources),
    }


def evaluate(context: ScreenContext, security_id: SecurityId) -> ScreenResultPayload:
    as_of = parse_ts(context.as_of)
    params = SCREEN_SPEC.parameters
    max_age = int(params["max_filing_age_days"])

    if is_unsupported_sector(context, security_id):
        return insufficient(["unsupported_sector"], _features(), 0.0)

    facts = active_facts(context, security_id)
    net_income = pick_ttm(facts_for(facts, NET_INCOME_CONCEPTS), as_of, max_age)
    ocf = pick_ttm(facts_for(facts, OCF_CONCEPTS), as_of, max_age)
    capex = pick_ttm(facts_for(facts, CAPEX_CONCEPTS), as_of, max_age)
    market_cap, bar, shares_fact = latest_market_cap(
        context, security_id, as_of, int(params["max_price_bar_age_trading_days"])
    )

    features = _features(net_income, ocf, capex, bar, shares_fact, market_cap)

    if net_income is None or ocf is None:
        return insufficient(["missing_financial_facts"], features, 0.0)
    if capex is None:
        # Absent/noncomparable capex is insufficient, never zero (design section 3).
        return insufficient(["noncomparable_capex"], features, 0.0)
    if market_cap is None or bar is None or shares_fact is None:
        if bar is None:
            reason = "missing_market_cap"
        elif shares_fact is None:
            reason = "missing_shares_fact"
        else:
            reason = "stale_price_bar"
        return insufficient([reason], features, 0.0)

    ni = float(net_income["value"])
    ocf_value = float(ocf["value"])
    capex_value = float(capex["value"])
    fcf = ocf_value - capex_value
    earnings_yield = ni / market_cap
    fcf_yield = fcf / market_cap
    features["earnings_yield_ttm"] = feature_value(
        round(earnings_yield, 6),
        "ratio",
        [fact_ref(net_income, "net_income_ttm"), bar_ref(bar, "close")],
    )
    features["fcf_yield_ttm"] = feature_value(
        round(fcf_yield, 6),
        "ratio",
        [fact_ref(ocf, "ocf_ttm"), fact_ref(capex, "capex_ttm"), bar_ref(bar, "close")],
    )
    features["market_cap"] = feature_value(
        round(market_cap, 2), "usd", [bar_ref(bar, "close"), fact_ref(shares_fact, "shares")]
    )

    pats = [fact.get("public_available_time") for fact in (net_income, ocf, capex, shares_fact)]
    pats.append(bar.get("public_available_time"))
    quality = min_freshness(pats, as_of, max_age)

    if params["require_positive_both"] and (ni <= 0 or fcf <= 0):
        # Nonpositive earnings/FCF is a genuine negative, not missing data.
        return negative(["nonpositive_earnings_or_fcf"], features, 1.0, quality)

    passed = earnings_yield >= float(params["min_earnings_yield"]) and fcf_yield >= float(
        params["min_fcf_yield"]
    )
    if passed:
        return positive(["yield_floors_met"], features, 1.0, quality)
    return negative(["yield_floors_not_met"], features, 1.0, quality)

"""informed_activity / informed_activity_open_market_buying / 1 (design section 3.4).

Pass iff the 90-day window shows enough qualifying open-market insider buying:
distinct buyers, aggregate purchase value, and value-to-market-cap all meet
floors.  Qualifying rows are code ``P``, acquired, direct, non-derivative;
effective amendments REPLACE (never add to) superseded originals.  Zero
purchases is a sufficient negative only when Form 4 daily-index coverage for
the window is provably complete; otherwise ``missing_form4_coverage``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from tradehub_research.hunters.common import (
    as_date,
    bar_ref,
    evidence_union,
    feature_value,
    insufficient,
    is_unsupported_sector,
    latest_market_cap,
    min_freshness,
    negative,
    parse_ts,
    positive,
)
from tradehub_research.screens import ScreenContext, ScreenResultPayload, ScreenSpec, SecurityId

SCREEN_SPEC = ScreenSpec(
    family="informed_activity",
    screen_id="informed_activity_open_market_buying",
    screen_version=1,
    feature_schema_version=1,
    parameters={
        "lookback_days": 90,
        "min_distinct_buyers": 2,
        "min_purchase_value": 100000.0,
        "min_value_to_market_cap": 0.0001,
        "require_all": True,
    },
    required_features=["form4_coverage", "qualifying_purchases", "market_cap"],
    implementation_id="hunters/informed_activity.py:v1",
)


def _window_dates(as_of_date, lookback_days: int) -> set[str]:
    start = as_of_date - timedelta(days=lookback_days)
    # EDGAR daily master indexes are published on business days. Requiring
    # nonexistent weekend indexes would make complete coverage impossible.
    return {
        day.isoformat()
        for offset in range(lookback_days + 1)
        if (day := start + timedelta(days=offset)).weekday() < 5
    }


def _active_form4(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Effective amendments replace superseded originals (never additive)."""
    superseded = {
        row.get("supersedes_evidence_id") for row in rows if row.get("supersedes_evidence_id")
    }
    return [
        row for row in rows if not row.get("withdrawn") and row.get("evidence_id") not in superseded
    ]


def _qualifies(row: dict[str, Any], window_start, as_of_date) -> bool:
    if row.get("transaction_code") != "P":
        return False
    if row.get("acquired_disposed") != "A":
        return False
    if row.get("direct_indirect") != "D":
        return False
    if row.get("derivative"):
        return False
    pat = row.get("public_available_time")
    if pat is None:
        return False
    pat_day = as_date(pat)
    if not (window_start <= pat_day <= as_of_date):
        return False
    shares = row.get("shares")
    price = row.get("price_per_share")
    return (
        isinstance(shares, (int, float))
        and isinstance(price, (int, float))
        and shares > 0
        and price > 0
    )


def _features(purchases: list[dict[str, Any]], market_cap, bar, coverage_complete: bool):
    purchase_sources = [
        {
            "value": round(float(p["shares"]) * float(p["price_per_share"]), 2),
            "unit": "usd",
            "role": "qualifying_purchase",
            "evidence_id": p.get("evidence_id"),
            "owner": p.get("owner_id"),
            "public_available_time": p.get("public_available_time"),
        }
        for p in purchases
    ]
    mcap_sources = [bar_ref(bar, "close")] if bar is not None else []
    return {
        "purchase_value_90d": feature_value(
            round(sum(float(p["shares"]) * float(p["price_per_share"]) for p in purchases), 2)
            if purchases
            else 0.0,
            "usd",
            purchase_sources,
        ),
        "distinct_buyers_90d": feature_value(
            len({p.get("owner_id") for p in purchases if p.get("owner_id")}),
            "count",
            purchase_sources,
        ),
        "purchase_value_to_market_cap": feature_value(None, "ratio", []),
        "market_cap": feature_value(market_cap, "usd", mcap_sources),
        "form4_coverage_complete": feature_value(coverage_complete, "bool", []),
    }


def evaluate(context: ScreenContext, security_id: SecurityId) -> ScreenResultPayload:
    as_of = parse_ts(context.as_of)
    params = SCREEN_SPEC.parameters
    lookback = int(params["lookback_days"])
    window_dates = _window_dates(as_of.date(), lookback)
    window_start = as_of.date() - timedelta(days=lookback)

    # Coverage completeness is carried in ScreenContext: the set of settled
    # EDGAR daily-index dates scanned for this security.  A window date without
    # a settled index entry makes coverage unprovable.
    covered = set(context.form4_coverage.get(security_id, frozenset()))
    coverage_complete = bool(covered) and window_dates <= covered

    rows = _active_form4(context.form4.get(security_id, []))
    purchases = [row for row in rows if _qualifies(row, window_start, as_of.date())]
    purchases.sort(key=lambda r: (r.get("public_available_time") or "", r.get("evidence_id") or ""))

    market_cap, bar, _shares = latest_market_cap(context, security_id, as_of, None)
    features = _features(purchases, market_cap, bar, coverage_complete)

    if not coverage_complete:
        # Zero purchases is a negative ONLY with complete coverage (design 3.4).
        return insufficient(["missing_form4_coverage"], features, 0.0)

    if not purchases:
        return negative(["no_qualifying_purchases"], features, 1.0, 1.0)

    if market_cap is None:
        return insufficient(["missing_market_cap"], features, 0.0)

    total_value = sum(float(p["shares"]) * float(p["price_per_share"]) for p in purchases)
    distinct_buyers = len({p.get("owner_id") for p in purchases if p.get("owner_id")})
    ratio = total_value / market_cap
    features["purchase_value_to_market_cap"] = feature_value(
        round(ratio, 8),
        "ratio",
        features["purchase_value_90d"]["sources"] + features["market_cap"]["sources"],
    )

    pats = [p.get("public_available_time") for p in purchases]
    quality = min_freshness(pats, as_of, int(params["lookback_days"]) * 2)

    checks = [
        distinct_buyers >= int(params["min_distinct_buyers"]),
        total_value >= float(params["min_purchase_value"]),
        ratio >= float(params["min_value_to_market_cap"]),
    ]
    passed = all(checks) if params["require_all"] else any(checks)
    if passed:
        return positive(["informed_buying_floors_met"], features, 1.0, quality)
    return negative(["informed_buying_floors_not_met"], features, 1.0, quality)


_ = evidence_union  # re-exported for family modules that need raw unions
_ = is_unsupported_sector  # informed activity evaluates all sectors in v1

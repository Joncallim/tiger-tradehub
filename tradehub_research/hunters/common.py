"""Shared primitives for the six Phase-1 Hunter implementations.

Every Hunter is a pure function over a preloaded, PIT-filtered ``ScreenContext``.
This module centralises the common rules from the design document so that the
individual family modules stay small and auditable:

* curated XBRL concept aliases (design section 3, ordered, never summed);
* TTM / annual fact selection over already PIT-filtered fact rows;
* PIT market cap (close x latest eligible shares fact, split-adjusted only by
  eligible actions);
* filing freshness and price-bar staleness floors;
* query-time as-of price adjustment (eligible actions only);
* the 20:15 America/New_York session-bar eligibility boundary.

No SQL, no network, no model calls live here or in any Hunter module.
"""

from __future__ import annotations

import calendar
import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tradehub_research.db import normalize_ts
from tradehub_research.screens import ScreenContext, ScreenResultPayload, SecurityId

NEW_YORK = ZoneInfo("America/New_York")
BAR_ELIGIBLE_HHMM = (20, 15)  # 20:15 America/New_York on the session date (design section 4)

# Ordered aliases, never summed (design section 3 "Curated XBRL concepts").
REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "Revenues",
)
NET_INCOME_CONCEPTS = ("NetIncomeLoss",)
OPERATING_INCOME_CONCEPTS = ("OperatingIncomeLoss",)
OCF_CONCEPTS = ("NetCashProvidedByUsedInOperatingActivities",)
CAPEX_CONCEPTS = ("PaymentsToAcquirePropertyPlantAndEquipment",)
ASSETS_CONCEPTS = ("Assets",)
SHARES_CONCEPTS = ("EntityCommonStockSharesOutstanding",)

UNSUPPORTED_SECTORS = ("banks", "insurance")  # banks/insurers: valuation/inflection/quality
REIT_UNSUPPORTED = ("reit", "reits")  # additionally unsupported for quality

MIN_DAYS_FOR_TTM = 300
FISCAL_YEAR_MIN_DAYS = 300
FISCAL_YEAR_MAX_DAYS = 400


def sha256_hex(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_ts(value: str) -> datetime:
    normalized = normalize_ts(value)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def as_date(value: str) -> date:
    return parse_ts(value).date() if "T" in value else date.fromisoformat(value)


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # Feb 29 -> Feb 28 on non-leap years
        return day.replace(year=day.year + years, day=28)


def session_close_utc(session_day: date) -> datetime:
    """Return the 20:15 America/New_York eligibility instant for a session date."""
    local = datetime(
        session_day.year,
        session_day.month,
        session_day.day,
        BAR_ELIGIBLE_HHMM[0],
        BAR_ELIGIBLE_HHMM[1],
        tzinfo=NEW_YORK,
    )
    return local.astimezone(timezone.utc)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5  # Monday=0 .. Friday=5


def count_trading_sessions(start: date, end: date) -> int:
    """Count weekday sessions in [start, end]; weekends are never sessions."""
    if end < start:
        return 0
    days = (end - start).days
    weeks, remainder = divmod(days + 1, 7)
    count = weeks * 5
    for offset in range(remainder):
        if is_weekday(add_days(start, weeks * 7 + offset)):
            count += 1
    return count


def bar_is_eligible(bar: dict[str, Any], as_of: datetime) -> bool:
    """A session bar is knowable only after its 20:15 ET publication boundary."""
    session = as_date(bar["session_date"])
    return as_of >= session_close_utc(session)


def trading_days_since(latest_session: date, as_of: datetime) -> int:
    """Weekday sessions strictly after ``latest_session`` up to the as-of date."""
    return count_trading_sessions(add_days(latest_session, 1), as_of.date())


# ---------------------------------------------------------------------------
# Fact selection
# ---------------------------------------------------------------------------


def _is_active(fact: dict[str, Any]) -> bool:
    return not fact.get("withdrawn") and not fact.get("superseded")


def active_facts(ctx: ScreenContext, security_id: SecurityId) -> list[dict[str, Any]]:
    return [fact for fact in ctx.facts.get(security_id, []) if _is_active(fact)]


def facts_for(
    facts: list[dict[str, Any]], concept_aliases: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Facts whose concept is in the ordered alias list, first alias wins.

    Aliases are an ordered preference, never a sum: if any fact matches the
    first alias, lower-priority aliases are ignored entirely.
    """
    for concept in concept_aliases:
        matched = [f for f in facts if f.get("concept") == concept]
        if matched:
            return matched
    return []


def filing_age_ok(fact: dict[str, Any], as_of: datetime, max_filing_age_days: int) -> bool:
    pat = fact.get("public_available_time")
    if pat is None:
        return False
    age = (as_of.date() - as_date(pat)).days
    return age <= max_filing_age_days


def _duration_days(fact: dict[str, Any]) -> int | None:
    start, end = fact.get("period_start"), fact.get("period_end")
    if not start or not end:
        return None
    return (as_date(end) - as_date(start)).days


def pick_ttm(
    facts: list[dict[str, Any]], as_of: datetime, max_filing_age_days: int
) -> dict[str, Any] | None:
    """Newest duration fact covering roughly a trailing year, fresh enough."""
    candidates = []
    for fact in facts:
        duration = _duration_days(fact)
        if duration is None or duration < MIN_DAYS_FOR_TTM:
            continue
        if not filing_age_ok(fact, as_of, max_filing_age_days):
            continue
        candidates.append(fact)
    if not candidates:
        return None
    candidates.sort(
        key=lambda f: (f.get("period_end") or "", f.get("public_available_time") or ""),
        reverse=True,
    )
    return candidates[0]


def fiscal_year_ocf(facts: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
    """Completed fiscal-year OCF facts (distinct period ends, newest first)."""
    yearly: dict[str, dict[str, Any]] = {}
    for fact in facts:
        duration = _duration_days(fact)
        if duration is None or not (FISCAL_YEAR_MIN_DAYS <= duration <= FISCAL_YEAR_MAX_DAYS):
            continue
        end = fact.get("period_end") or ""
        if not end or as_date(end) >= as_of.date():
            continue  # only completed fiscal years
        existing = yearly.get(end)
        if existing is None or (fact.get("public_available_time") or "") > (
            existing.get("public_available_time") or ""
        ):
            yearly[end] = fact
    return [yearly[end] for end in sorted(yearly, reverse=True)]


def instant_fact_at_or_before(
    facts: list[dict[str, Any]], as_of: datetime
) -> dict[str, Any] | None:
    """Newest instant fact (e.g. shares outstanding) whose period end and PAT
    are both not after ``as_of``."""
    candidates = []
    for fact in facts:
        end = fact.get("period_end")
        pat = fact.get("public_available_time")
        if not end or pat is None:
            continue
        if as_date(end) <= as_of.date() and parse_ts(pat) <= as_of:
            candidates.append(fact)
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f["period_end"], f["public_available_time"]), reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Price bars / adjustment
# ---------------------------------------------------------------------------


def eligible_bars(
    ctx: ScreenContext, security_id: SecurityId, as_of: datetime
) -> list[dict[str, Any]]:
    """Raw session bars whose 20:15 ET boundary has passed, session-ordered."""
    bars = [
        bar
        for bar in ctx.price_bars.get(security_id, [])
        if _is_active(bar) and bar_is_eligible(bar, as_of)
    ]
    bars.sort(key=lambda b: (b["session_date"], b.get("evidence_id") or ""))
    deduped: dict[str, dict[str, Any]] = {}
    for bar in bars:
        deduped[bar["session_date"]] = bar  # latest eligible version wins
    return [deduped[key] for key in sorted(deduped)]


def eligible_actions(
    ctx: ScreenContext, security_id: SecurityId, as_of: datetime
) -> list[dict[str, Any]]:
    """Split/dividend action records knowable at ``as_of`` (PAT-authoritative)."""
    actions = []
    for action in ctx.corporate_actions.get(security_id, []):
        if not _is_active(action):
            continue
        pat = action.get("public_available_time")
        if pat is None or parse_ts(pat) > as_of:
            continue
        actions.append(action)
    actions.sort(key=lambda a: (a.get("effective_date") or "", a.get("evidence_id") or ""))
    return actions


def adjusted_close_series(
    bars: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> list[tuple[str, float]]:
    """Query-time as-of adjustment of raw closes using only eligible actions.

    The returned series is expressed in units of the latest bar's share class:
    bars before a split are scaled down, dividend cash reinvested multiplicatively.
    """
    if not bars:
        return []
    series = [(bar["session_date"], float(bar["close"])) for bar in bars]
    for action in actions:
        effective = action.get("effective_date")
        if not effective:
            continue
        kind = action.get("action_type")
        factor = 0.0
        if kind == "split":
            factor = float(action.get("factor") or 0.0)
            if factor <= 0:
                continue
            multiplier = 1.0 / factor
        elif kind == "dividend":
            cash = float(action.get("cash") or 0.0)
            if cash < 0:
                continue
            prior = next((close for day, close in series if day < effective), None)
            if prior is None or prior <= 0:
                continue
            # Apply against each bar individually below; use per-bar cash/prior.
            adjusted = []
            for day, close in series:
                if day < effective and close > 0:
                    adjusted.append((day, close * max(close - cash, 0.0) / close))
                else:
                    adjusted.append((day, close))
            series = adjusted
            continue
        else:
            continue
        series = [
            (day, close * multiplier) if day < effective else (day, close) for day, close in series
        ]
    return series


def latest_market_cap(
    ctx: ScreenContext,
    security_id: SecurityId,
    as_of: datetime,
    max_bar_age_trading_days: int | None = None,
) -> tuple[float | None, dict[str, Any] | None, dict[str, Any] | None]:
    """PIT market cap = close x latest eligible shares fact, split-adjusted.

    Returns ``(market_cap, bar, shares_fact)``; any element may be ``None`` when
    the corresponding input is absent or stale.  Only split actions rescale the
    share count; dividends do not.
    """
    bars = eligible_bars(ctx, security_id, as_of)
    if not bars:
        return None, None, None
    bar = bars[-1]
    if max_bar_age_trading_days is not None:
        if trading_days_since(as_date(bar["session_date"]), as_of) > max_bar_age_trading_days:
            return None, bar, None
    shares_facts = facts_for(active_facts(ctx, security_id), SHARES_CONCEPTS)
    shares_fact = instant_fact_at_or_before(shares_facts, as_of)
    if shares_fact is None:
        return None, bar, None
    shares = shares_fact.get("value")
    if not isinstance(shares, (int, float)) or shares <= 0:
        return None, bar, None
    share_count = float(shares)
    fact_end = str(shares_fact["period_end"])
    for action in eligible_actions(ctx, security_id, as_of):
        if action.get("action_type") != "split":
            continue
        effective = action.get("effective_date")
        factor = float(action.get("factor") or 0.0)
        if effective and fact_end < effective <= bar["session_date"] and factor > 0:
            share_count *= factor
    market_cap = float(bar["close"]) * share_count
    return market_cap, bar, shares_fact


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------


def fact_ref(fact: dict[str, Any], role: str) -> dict[str, Any]:
    """Golden raw-feature lineage entry for a fact row."""
    return {
        "value": fact.get("value"),
        "unit": fact.get("unit"),
        "concept": fact.get("concept"),
        "period_start": fact.get("period_start"),
        "period_end": fact.get("period_end"),
        "public_available_time": fact.get("public_available_time"),
        "accession": fact.get("accession"),
        "role": role,
        "evidence_id": fact.get("evidence_id"),
    }


def bar_ref(bar: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "value": bar.get("close"),
        "unit": "usd_per_share",
        "session_date": bar.get("session_date"),
        "public_available_time": bar.get("public_available_time"),
        "role": role,
        "evidence_id": bar.get("evidence_id"),
    }


def feature_value(
    value: Any,
    unit: str,
    sources: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    """A raw-feature entry with explicit nulls, units and evidence lineage."""
    entry: dict[str, Any] = {"value": value, "unit": unit, "sources": sources}
    entry.update(extra)
    return entry


def evidence_union(features: dict[str, Any]) -> list[str]:
    ids: set[str] = set()
    for feature in features.values():
        if not isinstance(feature, dict):
            continue
        for source in feature.get("sources", []):
            evidence_id = source.get("evidence_id")
            if evidence_id:
                ids.add(evidence_id)
    return sorted(ids)


def insufficient(reason_codes: list[str], features: dict[str, Any], data_quality: float):
    return ScreenResultPayload(
        raw_features=features,
        evidence_ids=evidence_union(features),
        reason_codes=sorted(set(reason_codes)),
        sufficient_data=False,
        passed=False,
        confidence=0.0,
        data_quality=data_quality,
    )


def negative(reason_codes: list[str], features: dict[str, Any], confidence: float, quality: float):
    return ScreenResultPayload(
        raw_features=features,
        evidence_ids=evidence_union(features),
        reason_codes=sorted(set(reason_codes)),
        sufficient_data=True,
        passed=False,
        confidence=confidence,
        data_quality=quality,
    )


def positive(reason_codes: list[str], features: dict[str, Any], confidence: float, quality: float):
    return ScreenResultPayload(
        raw_features=features,
        evidence_ids=evidence_union(features),
        reason_codes=sorted(set(reason_codes)),
        sufficient_data=True,
        passed=True,
        confidence=confidence,
        data_quality=quality,
    )


def min_freshness(pats: list[str | None], as_of: datetime, max_age_days: int) -> float:
    """Freshness component of data_quality: 1.0 when all inputs are fresh."""
    worst = 1.0
    for pat in pats:
        if pat is None:
            return 0.0
        age = (as_of.date() - as_date(pat)).days
        worst = min(worst, max(0.0, 1.0 - age / max(max_age_days * 4, 1)))
    return round(worst, 6)


def is_unsupported_sector(ctx: ScreenContext, security_id: SecurityId, extra=()) -> bool:
    sector = (ctx.sectors.get(security_id) or "").strip().lower()
    return sector in set(UNSUPPORTED_SECTORS) | set(extra)


def last_day_of_month(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])

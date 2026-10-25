"""Read-only provenance loader for the inactive valuation-headroom metric.

This is deliberately not a screen adapter.  It reads the append-only evidence
ledger only when a caller explicitly asks for a paper review input, and refuses
to manufacture precision or lineage from the lossy active ``ScreenContext``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.hunters.common import instant_fact_at_or_before, parse_ts, pick_ttm
from tradehub_research.hunters.valuation_headroom import (
    EvidenceIdentity,
    HeadroomInput,
    SplitLineage,
    evaluate,
)


class _NumericLiteral(str):
    """A JSON number token, distinct from an explicitly serialized decimal."""


@dataclass(frozen=True)
class _Record:
    identity: EvidenceIdentity
    fields: dict[str, Any]


def _fields(value: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value, parse_float=_NumericLiteral, parse_int=_NumericLiteral)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _decimal(value: Any) -> Decimal | None:
    """Accept only a quoted finite decimal; JSON float/int literals are rejected."""
    if not isinstance(value, str) or isinstance(value, _NumericLiteral):
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _record(row: Any) -> _Record | None:
    fields = _fields(row["structured_fields"])
    if fields is None:
        return None
    required = ("evidence_id", "source_id", "content_hash", "public_available_time")
    if not all(isinstance(row[key], str) and row[key] for key in required):
        return None
    fields = dict(fields)
    fields["public_available_time"] = row["public_available_time"]
    if fields.get("record_type") == "xbrl_fact":
        fields.setdefault("period_start", fields.get("start"))
        fields.setdefault("period_end", fields.get("end"))
    return _Record(
        EvidenceIdentity(*(str(row[key]) for key in required)),
        fields,
    )


def _fact(
    records: list[_Record], metric: str, cutoff: str, *, instant: bool = False
) -> _Record | None:
    matched = [
        item
        for item in records
        if item.fields.get("record_type") == "xbrl_fact" and item.fields.get("metric") == metric
    ]
    selected = (
        instant_fact_at_or_before([item.fields for item in matched], parse_ts(cutoff))
        if instant
        else pick_ttm([item.fields for item in matched], parse_ts(cutoff), 150)
    )
    return next((item for item in matched if item.fields is selected), None)


def _unknown_input(
    security_id: str, evidence_cutoff: str, expected_price_session: str
) -> HeadroomInput:
    return HeadroomInput(
        security_id=security_id,
        evidence_cutoff=evidence_cutoff,
        expected_price_session=expected_price_session,
        price_session="",
        net_income_ttm=None,
        free_cash_flow_ttm=None,
        price=None,
        shares_outstanding=None,
        currency=None,
        net_income_lineage=None,
        operating_cash_flow_lineage=None,
        capex_lineage=None,
        price_lineage=None,
        shares_lineage=None,
        split_lineage=None,
        currency_lineage=None,
    )


def load_input(
    database: ResearchDB,
    *,
    security_id: str,
    evidence_cutoff: str,
    expected_price_session: str,
) -> HeadroomInput:
    """Build a provenance-complete input from persisted evidence or fail closed.

    The ledger must carry quoted decimal strings and explicit ``currency_basis``
    and ``split_coverage`` records.  Current adapters do not emit the latter
    two records (and Tiingo prices are floats), so their data intentionally
    remains UNKNOWN until a separately reviewed ingestion contract supplies it.
    """
    fallback = _unknown_input(security_id, evidence_cutoff, expected_price_session)
    rows = EvidenceStore(database).historical(evidence_cutoff, security_id)
    records = [item for row in rows if (item := _record(row)) is not None]

    net_income = _fact(records, "net_income", evidence_cutoff)
    operating_cash_flow = _fact(records, "operating_cash_flow", evidence_cutoff)
    capex = _fact(records, "capex", evidence_cutoff)
    shares = _fact(records, "shares_outstanding", evidence_cutoff, instant=True)
    prices = [
        item
        for item in records
        if item.fields.get("record_type") == "price_bar"
        and item.fields.get("session_date") == expected_price_session
    ]
    price = max(
        prices,
        key=lambda item: (item.identity.public_available_time, item.identity.evidence_id),
        default=None,
    )
    if not all((net_income, operating_cash_flow, capex, shares, price)):
        return fallback

    assert net_income and operating_cash_flow and capex and shares and price
    net_income_value = _decimal(net_income.fields.get("value"))
    operating_cash_flow_value = _decimal(operating_cash_flow.fields.get("value"))
    capex_value = _decimal(capex.fields.get("value"))
    price_value = _decimal(price.fields.get("close"))
    shares_value = _decimal(shares.fields.get("value"))
    if None in (
        net_income_value,
        operating_cash_flow_value,
        capex_value,
        price_value,
        shares_value,
    ):
        return fallback
    if not all(
        value > 0
        for value in (
            net_income_value,
            operating_cash_flow_value,
            capex_value,
            price_value,
            shares_value,
        )
    ):
        return fallback

    shares_period_end = shares.fields.get("period_end")
    if not isinstance(shares_period_end, str):
        return fallback
    splits = [
        item
        for item in records
        if item.fields.get("record_type") == "split"
        and isinstance(item.fields.get("effective_date"), str)
        and shares_period_end < item.fields["effective_date"] <= expected_price_session
    ]
    action_ids = sorted(item.identity.evidence_id for item in splits)
    selected_ids = sorted(
        item.identity.evidence_id
        for item in (net_income, operating_cash_flow, capex, shares, price)
    )
    coverage = next(
        (
            item
            for item in records
            if item.fields.get("record_type") == "split_coverage"
            and item.fields.get("covered_from") == shares_period_end
            and item.fields.get("covered_through") == expected_price_session
            and item.fields.get("action_evidence_ids") == action_ids
        ),
        None,
    )
    currency = next(
        (
            item
            for item in records
            if item.fields.get("record_type") == "currency_basis"
            and item.fields.get("currency") == "USD"
            and sorted(item.fields.get("evidence_ids", [])) == selected_ids
        ),
        None,
    )
    if coverage is None or currency is None:
        return fallback
    split_values = [_decimal(item.fields.get("factor")) for item in splits]
    if any(value is None or value <= 0 for value in split_values):
        return fallback
    factor = Decimal(1)
    for value in split_values:
        assert value is not None
        factor *= value
    return HeadroomInput(
        security_id=security_id,
        evidence_cutoff=evidence_cutoff,
        expected_price_session=expected_price_session,
        price_session=expected_price_session,
        net_income_ttm=net_income_value,
        free_cash_flow_ttm=operating_cash_flow_value - capex_value,
        price=price_value,
        shares_outstanding=shares_value,
        currency="USD",
        net_income_lineage=net_income.identity,
        operating_cash_flow_lineage=operating_cash_flow.identity,
        capex_lineage=capex.identity,
        price_lineage=price.identity,
        shares_lineage=shares.identity,
        split_lineage=SplitLineage(
            coverage.identity, tuple(item.identity for item in splits), factor
        ),
        currency_lineage=currency.identity,
    )


def evaluate_persisted_input(
    database: ResearchDB,
    *,
    security_id: str,
    evidence_cutoff: str,
    expected_price_session: str,
):
    """Evaluate a caller-requested paper input; this never writes or registers anything."""
    return evaluate(
        load_input(
            database,
            security_id=security_id,
            evidence_cutoff=evidence_cutoff,
            expected_price_session=expected_price_session,
        )
    )

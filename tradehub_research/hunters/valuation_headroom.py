"""Inactive, paper-only valuation-screen headroom metric.

This module deliberately is *not* imported by :mod:`tradehub_research.hunters`
and does not register a screen.  It prepares a deterministic BUY-qualification
input for review; it is neither a fair-value estimate nor a sell signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from tradehub_research.db import normalize_ts
from tradehub_research.hunters.common import session_close_utc, trading_days_since
from tradehub_research.hunters.valuation import SCREEN_SPEC as VALUATION_SCREEN_SPEC
from tradehub_research.portfolio.snapshot import SignalInput, build_signal_input
from tradehub_research.screens import canonical_json

METHOD_ID = "valuation-screen-headroom"
METHOD_VERSION = 2
EARNINGS_YIELD_FLOOR = Decimal("0.05")
FCF_YIELD_FLOOR = Decimal("0.03")
PPM = Decimal("1000000")
MAX_FUNDAMENTAL_AGE_DAYS = int(VALUATION_SCREEN_SPEC.parameters["max_filing_age_days"])
MAX_PRICE_BAR_AGE_TRADING_DAYS = int(
    VALUATION_SCREEN_SPEC.parameters["max_price_bar_age_trading_days"]
)


@dataclass(frozen=True)
class EvidenceIdentity:
    """An explicit immutable identity for one raw evidence item."""

    evidence_id: str
    source_id: str
    content_hash: str
    public_available_time: str


@dataclass(frozen=True)
class SplitLineage:
    """Coverage record plus every visible split used to adjust share count.

    ``coverage`` is required even when ``actions`` is empty.  It is an explicit
    source-backed statement that the no-action chain was checked, rather than a
    boolean assertion or an accidental equality of missing dictionaries.
    """

    coverage: EvidenceIdentity | None
    actions: tuple[EvidenceIdentity, ...]
    cumulative_factor: Decimal | None


@dataclass(frozen=True)
class HeadroomInput:
    security_id: str
    evidence_cutoff: str
    expected_price_session: str
    price_session: str
    net_income_ttm: Decimal | None
    free_cash_flow_ttm: Decimal | None
    price: Decimal | None
    shares_outstanding: Decimal | None
    currency: str | None
    net_income_lineage: EvidenceIdentity | None
    operating_cash_flow_lineage: EvidenceIdentity | None
    capex_lineage: EvidenceIdentity | None
    price_lineage: EvidenceIdentity | None
    shares_lineage: EvidenceIdentity | None
    split_lineage: SplitLineage | None
    currency_lineage: EvidenceIdentity | None


@dataclass(frozen=True)
class HeadroomResult:
    security_id: str
    as_of: str
    status: str
    remaining_opportunity_ppm: int | None
    boundary: Decimal | None
    market_cap: Decimal | None
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    method_id: str
    method_version: int
    config_hash: str
    raw_evidence_identity: str

    def to_signal_input(self) -> SignalInput:
        """Use the existing persisted signal-input contract; no new table."""
        return build_signal_input(
            self.security_id,
            self.as_of,
            remaining_opportunity_ppm=self.remaining_opportunity_ppm,
            opportunity_status=self.status,
            source_kind="DETERMINISTIC_METRIC",
            evidence_ids=list(self.evidence_ids),
        )


def _identity_dict(value: Any) -> dict[str, str | None]:
    if not isinstance(value, EvidenceIdentity):
        return {
            "evidence_id": None,
            "source_id": None,
            "content_hash": None,
            "public_available_time": None,
        }
    return {
        "evidence_id": value.evidence_id,
        "source_id": value.source_id,
        "content_hash": value.content_hash,
        "public_available_time": value.public_available_time,
    }


def _config_material() -> dict[str, Any]:
    return {
        "method_id": METHOD_ID,
        "method_version": METHOD_VERSION,
        "earnings_yield_floor": str(EARNINGS_YIELD_FLOOR),
        "fcf_yield_floor": str(FCF_YIELD_FLOOR),
        "max_fundamental_age_days": MAX_FUNDAMENTAL_AGE_DAYS,
        "max_price_bar_age_trading_days": MAX_PRICE_BAR_AGE_TRADING_DAYS,
        "currency": "USD",
        "price_session_rule": "expected session, 20:15 America/New_York cutoff",
    }


def _hash(material: dict[str, Any]) -> str:
    from hashlib import sha256

    return sha256(canonical_json(material).encode("utf-8")).hexdigest()


CONFIG_HASH = _hash(_config_material())


def _raw_identity(value: HeadroomInput) -> str:
    split = value.split_lineage if isinstance(value.split_lineage, SplitLineage) else None
    material = {
        "security_id": value.security_id,
        "evidence_cutoff": value.evidence_cutoff,
        "expected_price_session": value.expected_price_session,
        "price_session": value.price_session,
        "net_income_ttm": str(value.net_income_ttm),
        "free_cash_flow_ttm": str(value.free_cash_flow_ttm),
        "price": str(value.price),
        "shares_outstanding": str(value.shares_outstanding),
        "currency": value.currency,
        "net_income": _identity_dict(value.net_income_lineage),
        "operating_cash_flow": _identity_dict(value.operating_cash_flow_lineage),
        "capex": _identity_dict(value.capex_lineage),
        "price_lineage": _identity_dict(value.price_lineage),
        "shares": _identity_dict(value.shares_lineage),
        "currency_lineage": _identity_dict(value.currency_lineage),
        "split_coverage": _identity_dict(None if split is None else split.coverage),
        "split_actions": (
            [] if split is None else [_identity_dict(item) for item in split.actions]
        ),
        "split_factor": None if split is None else str(split.cumulative_factor),
    }
    return _hash(material)


def _positive_finite(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _parse_aware(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_identity(value: Any, cutoff: datetime) -> bool:
    if not isinstance(value, EvidenceIdentity) or not all(
        isinstance(item, str) and item.strip()
        for item in (
            value.evidence_id,
            value.source_id,
            value.content_hash,
            value.public_available_time,
        )
    ):
        return False
    published = _parse_aware(value.public_available_time)
    return published is not None and published <= cutoff


def _evidence_ids(value: HeadroomInput) -> tuple[str, ...]:
    identities = [
        value.net_income_lineage,
        value.operating_cash_flow_lineage,
        value.capex_lineage,
        value.price_lineage,
        value.shares_lineage,
        value.currency_lineage,
    ]
    if isinstance(value.split_lineage, SplitLineage):
        identities.append(value.split_lineage.coverage)
        identities.extend(value.split_lineage.actions)
    return tuple(
        sorted(
            {
                item.evidence_id
                for item in identities
                if isinstance(item, EvidenceIdentity) and item.evidence_id
            }
        )
    )


def _unknown(value: HeadroomInput, *reasons: str) -> HeadroomResult:
    return HeadroomResult(
        security_id=value.security_id,
        as_of=(
            normalize_ts(value.evidence_cutoff)
            if _parse_aware(value.evidence_cutoff)
            else value.evidence_cutoff
        ),
        status="UNKNOWN",
        remaining_opportunity_ppm=None,
        boundary=None,
        market_cap=None,
        reason_codes=tuple(sorted(set(reasons))),
        evidence_ids=_evidence_ids(value),
        method_id=METHOD_ID,
        method_version=METHOD_VERSION,
        config_hash=CONFIG_HASH,
        raw_evidence_identity=_raw_identity(value),
    )


def evaluate(value: HeadroomInput) -> HeadroomResult:
    """Measure a discount to the existing valuation-screen boundary.

    ``B = min(net_income_ttm / .05, free_cash_flow_ttm / .03)`` and
    ``H = floor(1_000_000 * max(0, 1 - market_cap / B))``.  Unknown input is
    deliberately never coerced to zero.  A valid cap at/above the boundary is
    a measured zero and remains a BUY-only qualification input.
    """
    if not isinstance(value.security_id, str) or not value.security_id.strip():
        return _unknown(value, "invalid_security_id")
    cutoff = _parse_aware(value.evidence_cutoff)
    if cutoff is None:
        return _unknown(value, "invalid_evidence_cutoff")
    try:
        expected_session = date.fromisoformat(value.expected_price_session)
        actual_session = date.fromisoformat(value.price_session)
    except (TypeError, ValueError):
        return _unknown(value, "invalid_price_session")

    reasons: list[str] = []
    if actual_session != expected_session:
        reasons.append("price_session_mismatch")
    if cutoff < session_close_utc(expected_session):
        reasons.append("price_session_not_published")
    if trading_days_since(actual_session, cutoff) > MAX_PRICE_BAR_AGE_TRADING_DAYS:
        reasons.append("stale_price_bar")
    if value.currency != "USD":
        reasons.append("unsupported_currency")
    split = value.split_lineage if isinstance(value.split_lineage, SplitLineage) else None
    if not all(
        _positive_finite(item)
        for item in (
            value.net_income_ttm,
            value.free_cash_flow_ttm,
            value.price,
            value.shares_outstanding,
            None if split is None else split.cumulative_factor,
        )
    ):
        reasons.append("invalid_raw_operands")

    identities = (
        value.net_income_lineage,
        value.operating_cash_flow_lineage,
        value.capex_lineage,
        value.price_lineage,
        value.shares_lineage,
        value.currency_lineage,
        None if split is None else split.coverage,
    )
    if not all(_valid_identity(item, cutoff) for item in identities):
        reasons.append("incomplete_or_future_lineage")
    if split is None or not all(_valid_identity(item, cutoff) for item in split.actions):
        reasons.append("incomplete_or_future_lineage")

    for fact in (
        value.net_income_lineage,
        value.operating_cash_flow_lineage,
        value.capex_lineage,
    ):
        published = (
            _parse_aware(fact.public_available_time) if isinstance(fact, EvidenceIdentity) else None
        )
        if (
            published is not None
            and (cutoff.date() - published.date()).days > MAX_FUNDAMENTAL_AGE_DAYS
        ):
            reasons.append("stale_fundamentals")
    if reasons:
        return _unknown(value, *reasons)

    assert value.net_income_ttm is not None
    assert value.free_cash_flow_ttm is not None
    assert value.price is not None
    assert value.shares_outstanding is not None
    assert split is not None and split.cumulative_factor is not None
    boundary = min(
        value.net_income_ttm / EARNINGS_YIELD_FLOOR,
        value.free_cash_flow_ttm / FCF_YIELD_FLOOR,
    )
    market_cap = value.price * value.shares_outstanding * split.cumulative_factor
    headroom = max(Decimal(0), Decimal(1) - market_cap / boundary)
    ppm = int((PPM * headroom).to_integral_value(rounding=ROUND_FLOOR))
    return HeadroomResult(
        security_id=value.security_id,
        as_of=normalize_ts(value.evidence_cutoff),
        status="KNOWN",
        remaining_opportunity_ppm=ppm,
        boundary=boundary,
        market_cap=market_cap,
        reason_codes=("valuation_boundary_headroom",),
        evidence_ids=_evidence_ids(value),
        method_id=METHOD_ID,
        method_version=METHOD_VERSION,
        config_hash=CONFIG_HASH,
        raw_evidence_identity=_raw_identity(value),
    )

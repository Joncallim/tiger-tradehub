"""Offline acceptance for the inactive valuation-screen headroom metric."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tradehub_research.hunters.valuation_headroom import (
    EvidenceIdentity,
    HeadroomInput,
    SplitLineage,
    evaluate,
)
from tradehub_research.screens import registered_screens


def _evidence(name: str, published: str = "2025-01-04T16:00:00Z") -> EvidenceIdentity:
    return EvidenceIdentity(
        evidence_id=f"e:{name}",
        source_id=f"source:{name}",
        content_hash=f"hash:{name}",
        public_available_time=published,
    )


def _input(**updates) -> HeadroomInput:
    values = {
        "security_id": "security-a",
        # 20:15 America/New_York on 2025-06-02 is 00:15Z on June 3.
        "evidence_cutoff": "2025-06-03T01:00:00Z",
        "expected_price_session": "2025-06-02",
        "price_session": "2025-06-02",
        "net_income_ttm": Decimal("50"),
        "free_cash_flow_ttm": Decimal("30"),
        "price": Decimal("1"),
        "shares_outstanding": Decimal("800"),
        "currency": "USD",
        "net_income_lineage": _evidence("net-income"),
        "free_cash_flow_lineage": _evidence("fcf"),
        "price_lineage": _evidence("price", "2025-06-03T00:20:00Z"),
        "shares_lineage": _evidence("shares"),
        "split_lineage": SplitLineage(_evidence("split-coverage"), (), Decimal("1")),
        "currency_lineage": _evidence("currency"),
    }
    values.update(updates)
    return HeadroomInput(**values)


def test_metric_remains_unregistered_and_inactive():
    assert all(
        spec.screen_id != "valuation_screen_headroom"
        for spec, _ in registered_screens()
    )


def test_boundary_uses_the_limiting_yield_and_exact_buy_thresholds():
    earnings_limited = evaluate(_input(free_cash_flow_ttm=Decimal("60")))
    fcf_limited = evaluate(_input(net_income_ttm=Decimal("100")))
    assert (earnings_limited.boundary, fcf_limited.boundary) == (
        Decimal("1000"),
        Decimal("1000"),
    )

    at_threshold = evaluate(
        _input(price=Decimal("1"), shares_outstanding=Decimal("800"))
    )
    just_below = evaluate(
        _input(price=Decimal("800.001"), shares_outstanding=Decimal("1"))
    )
    at_or_above_boundary = evaluate(
        _input(price=Decimal("1000"), shares_outstanding=Decimal("1"))
    )
    assert at_threshold.remaining_opportunity_ppm == 200_000
    assert just_below.remaining_opportunity_ppm == 199_999
    assert at_or_above_boundary.remaining_opportunity_ppm == 0
    assert at_or_above_boundary.status == "KNOWN"  # measured zero, not UNKNOWN


def test_same_day_pit_and_quarterly_filing_age_are_explicit():
    same_day = evaluate(
        _input(
            evidence_cutoff="2025-06-03T00:15:00Z",
            price_lineage=_evidence("price", "2025-06-03T00:15:00Z"),
        )
    )
    quarterly = evaluate(
        _input(net_income_lineage=_evidence("net-income", "2025-03-05T16:00:00Z"))
    )
    stale = evaluate(
        _input(net_income_lineage=_evidence("net-income", "2025-01-03T16:00:00Z"))
    )
    stale_price = evaluate(
        _input(expected_price_session="2025-05-20", price_session="2025-05-20")
    )
    assert same_day.status == quarterly.status == "KNOWN"
    assert (stale.status, stale.reason_codes) == ("UNKNOWN", ("stale_fundamentals",))
    assert stale_price.status == "UNKNOWN"
    assert "stale_price_bar" in stale_price.reason_codes


def test_invalid_metadata_and_operands_are_unknown_not_a_fake_zero():
    invalid_operand = evaluate(_input(net_income_ttm=Decimal("NaN")))
    missing_split_coverage = evaluate(
        _input(split_lineage=SplitLineage(None, (), Decimal("1")))
    )
    malformed_price_metadata = evaluate(_input(price_lineage={}))
    naive_cutoff = evaluate(_input(evidence_cutoff="2025-06-03T01:00:00"))
    naive_pat = evaluate(
        _input(price_lineage=_evidence("price", "2025-06-03T00:20:00"))
    )
    future_currency = evaluate(
        _input(currency_lineage=_evidence("currency", "2025-06-03T02:00:00Z"))
    )
    wrong_currency = evaluate(_input(currency="CAD"))
    for result in (
        invalid_operand,
        missing_split_coverage,
        malformed_price_metadata,
        naive_cutoff,
        naive_pat,
        future_currency,
        wrong_currency,
    ):
        assert result.status == "UNKNOWN"
        assert result.remaining_opportunity_ppm is None


def test_raw_identity_changes_and_replay_is_deterministic():
    original = _input()
    replay = evaluate(original)
    changed = evaluate(
        replace(
            original,
            price_lineage=replace(
                original.price_lineage,
                content_hash="hash:revised-price",
            ),
        )
    )
    assert replay == evaluate(original)
    assert replay.raw_evidence_identity != changed.raw_evidence_identity
    # Identity is not inferred from IDs alone.
    assert replay.evidence_ids == changed.evidence_ids

"""Persisted-input acceptance for the inactive valuation headroom metric."""

from __future__ import annotations

from decimal import Decimal

from tests.portfolio_test_helpers import seed_security
from tests.test_valuation_headroom import _input
from tradehub_research.db import ResearchDB
from tradehub_research.hunters.valuation_headroom import evaluate
from tradehub_research.ops.decision_pipeline import load_persisted_signal_inputs
from tradehub_research.portfolio.snapshot import SnapshotStore


def test_signal_input_supersession_is_pit_partitioned_and_zero_does_not_sell(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as db:
        for security_id in ("security-a", "security-b", "security-future"):
            seed_security(db, security_id)
    store = SnapshotStore(database)

    known_a = evaluate(_input(security_id="security-a", evidence_cutoff="2025-06-03T01:00:00Z"))
    unknown_a = evaluate(
        _input(
            security_id="security-a",
            evidence_cutoff="2025-06-04T01:00:00Z",
            currency="CAD",
        )
    )
    zero_b = evaluate(
        _input(
            security_id="security-b",
            evidence_cutoff="2025-06-03T01:00:00Z",
            price=Decimal("1000"),
            shares_outstanding=Decimal("1"),
        )
    )
    future = evaluate(
        _input(
            security_id="security-future",
            evidence_cutoff="2025-06-10T01:00:00Z",
        )
    )
    for result in (known_a, unknown_a, zero_b, future):
        store.save_signal_input(result.to_signal_input(), recorded_at=result.as_of)

    signals = load_persisted_signal_inputs(database, decision_as_of="2025-06-05T00:00:00Z")
    by_security = {signal.security_id: signal for signal in signals}
    assert set(by_security) == {"security-a", "security-b"}
    assert by_security["security-a"].opportunity_status.value == "UNKNOWN"  # newer UNKNOWN wins
    assert by_security["security-b"].remaining_opportunity_ppm == 0
    assert by_security["security-b"].source_kind.value == "DETERMINISTIC_METRIC"

    # Existing policy has no WATCH->sell rule, and the zero is therefore not a
    # sell trigger.  It only fails the BUY-side 200,000 ppm entry floor.
    from tradehub_research.portfolio.eligibility import evaluate_eligibility
    from tradehub_research.portfolio.fixtures import fixture_policy
    from tradehub_research.portfolio.types import State

    decision = evaluate_eligibility(
        fixture_policy(),
        State.WATCH,
        {
            "conviction_ppm": 800_000,
            "data_quality_ppm": 900_000,
            "agreement_ppm": 800_000,
            "trajectory": "RISING",
            "position_present": False,
            "opportunity_ppm": by_security["security-b"].remaining_opportunity_ppm,
            "opportunity_known": True,
            "sector_coverage_status": "SUPPORTED",
        },
    )
    assert decision.status == "INELIGIBLE"
    assert decision.to_state is None  # WATCH is preserved; no zero-triggered sell exists.

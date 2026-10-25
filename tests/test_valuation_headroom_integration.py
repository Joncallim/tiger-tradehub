"""Persisted-input acceptance for the inactive valuation headroom metric."""

from __future__ import annotations

from decimal import Decimal

from tests.portfolio_test_helpers import (
    seed_pipeline_run,
    seed_price_bars,
    seed_score,
    seed_security,
)
from tests.test_valuation_headroom import _input
from tradehub_research.db import ResearchDB
from tradehub_research.hunters.valuation_headroom import evaluate
from tradehub_research.ops.decision_pipeline import load_persisted_signal_inputs
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.fixtures import fixture_policy
from tradehub_research.portfolio.policy import PolicyRegistry
from tradehub_research.portfolio.snapshot import SnapshotStore
from tradehub_research.portfolio.snapshot import build_snapshot


def test_signal_input_supersession_is_pit_partitioned_and_zero_does_not_sell(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as db:
        for security_id in ("security-a", "security-b", "security-future"):
            seed_security(db, security_id)
        bar_ids = seed_price_bars(
            db,
            "security-b",
            closes=[50.0] * 40,
            start_date="2025-04-23",
        )
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

    # Prove the real persisted portfolio path: zero can create WATCH (which
    # does not require opportunity), then cannot enter or sell from WATCH.
    PolicyRegistry(database).register(fixture_policy())
    engine = PortfolioEngine(database)

    def snapshot(as_of: str):
        return build_snapshot(
            as_of,
            cash_microusd=10_000_000_000,
            nav_microusd=10_000_000_000,
            market_inputs=[
                {
                    "security_id": "security-b",
                    "mark_price_microusd": 50_000_000,
                    "price_as_of": as_of,
                    "avg_dollar_volume_microusd": 51_450_000_000_000,
                    "liquidity_as_of": as_of,
                    "evidence_ids": bar_ids,
                }
            ],
        )

    first_as_of = "2025-06-03T01:00:00Z"
    second_as_of = "2025-06-04T01:00:00Z"
    with database.connect() as db:
        seed_pipeline_run(db, "watch-run", first_as_of)
        seed_score(
            db,
            pipeline_run_id="watch-run",
            security_id="security-b",
            run_as_of=first_as_of,
        )
    first = engine.run(
        pipeline_run_id="watch-run",
        policy_version="fixture-policy-v1",
        snapshot=snapshot(first_as_of),
        decision_as_of=first_as_of,
        signals=[by_security["security-b"]],
        allow_fixture=True,
    )
    assert first.transition_count == 1

    with database.connect() as db:
        seed_pipeline_run(db, "zero-run", second_as_of)
        seed_score(
            db,
            pipeline_run_id="zero-run",
            security_id="security-b",
            run_as_of=second_as_of,
            suffix="zero",
        )
    second = engine.run(
        pipeline_run_id="zero-run",
        policy_version="fixture-policy-v1",
        snapshot=snapshot(second_as_of),
        decision_as_of=second_as_of,
        signals=[by_security["security-b"]],
        allow_fixture=True,
    )
    assert (second.transition_count, second.proposal_count) == (0, 0)
    with database.connect(read_only=True) as db:
        state = db.execute(
            "SELECT state FROM portfolio_state WHERE security_id='security-b'"
        ).fetchone()["state"]
        exits = db.execute(
            "SELECT count(*) FROM trade_proposal WHERE action='SELL'"
        ).fetchone()[0]
    assert state == "WATCH"
    assert exits == 0

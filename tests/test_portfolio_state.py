"""State machine: canonical edges, derivation, persistence, settlement, cooldown."""

from __future__ import annotations

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.state import (
    cooldown_satisfied,
    current_state,
    pending_resolution,
    persistence_count,
)
from tradehub_research.portfolio.types import State
from tests.portfolio_test_helpers import seed_security, seed_pipeline_run, seed_score

SNAP_ID = "s" * 64
SCORE_ID = "c" * 64
PORTFOLIO_RUN_ID = "r" * 64


def _db(tmp_path) -> ResearchDB:
    from tradehub_research.portfolio.fixtures import fixture_policy
    from tradehub_research.portfolio.policy import PolicyRegistry

    global SNAP_ID, SCORE_ID, PORTFOLIO_RUN_ID
    database = ResearchDB(tmp_path / "state.db")
    database.migrate()
    PolicyRegistry(database).register(fixture_policy())
    with database.connect() as db:
        seed_security(db, "sec1")
        seed_pipeline_run(db, "run1", "2025-06-01T00:00:00Z")
        SCORE_ID = seed_score(db, pipeline_run_id="run1", security_id="sec1", committee_suffix="a")
        SNAP_ID = "s" * 64
        PORTFOLIO_RUN_ID = "r" * 64
        db.execute(
            "INSERT INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,cash_status,"
            "nav_microusd,valuation_status,holdings_status,provenance_json,input_hash,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s" * 64,
                "2025-06-01T00:00:00Z",
                "USD",
                10000000000,
                "KNOWN",
                10000000000,
                "KNOWN",
                "KNOWN",
                "{}",
                "i" * 64,
                "2025-06-01T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO portfolio_run(run_id,pipeline_run_id,decision_as_of,portfolio_snapshot_id,"
            "policy_version,score_set_hash,signal_set_hash,candidate_set_hash,invocation_key,"
            "state_prestate_hash,market_data_prestate_hash,budget_prestate_hash,input_hash,"
            "expected_security_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                PORTFOLIO_RUN_ID,
                "run1",
                "2025-06-01T00:00:00Z",
                SNAP_ID,
                "fixture-policy-v1",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "g" * 64,
                "h" * 64,
                1,
                "2025-06-01T00:00:00Z",
            ),
        )
    return database


def _pad(value: str, length: int = 64) -> str:
    return value.ljust(length, "0")


def _insert_observation(
    db,
    *,
    security_id="sec1",
    as_of="2025-06-01T00:00:00Z",
    decision_id="d1",
    evidence_hash="H1",
    evidence_driven=1,
    signal_state="ENTER",
    signal_status="PASS",
    policy_version="fixture-policy-v1",
):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_state_observation("
            "decision_id,run_id,security_id,current_state,signal_state,proposed_state,"
            "portfolio_snapshot_id,policy_version,scored_evidence_hash,change_cause,"
            "evidence_driven,signal_status,persistence_count_at_decision,persistence_required,"
            "material_change_satisfied,cooldown_satisfied,risk_status,final_status,"
            "reason_codes_json,risk_json,sizing_json,decision_input_hash,observed_at,recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _pad(decision_id),
                PORTFOLIO_RUN_ID,
                security_id,
                "WATCH",
                signal_state,
                signal_state,
                SNAP_ID,
                policy_version,
                evidence_hash,
                "EVIDENCE_DRIVEN",
                evidence_driven,
                signal_status,
                0,
                0,
                0,
                1,
                "PASS",
                "NO_ACTION",
                "[]",
                "{}",
                "{}",
                _pad("obs-hash", 64),
                as_of,
                as_of,
            ),
        )
        return _pad(decision_id)


def _insert_transition(
    db,
    *,
    security_id="sec1",
    from_state="DISCOVER",
    to_state="WATCH",
    effective_at="2025-06-01T00:00:00Z",
    decision_id="d0",
    transition_id="t0",
):
    decision_key = _pad(decision_id)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_state_observation("
            "decision_id,run_id,security_id,current_state,signal_state,proposed_state,"
            "portfolio_snapshot_id,policy_version,evidence_driven,signal_status,"
            "persistence_count_at_decision,persistence_required,material_change_satisfied,"
            "cooldown_satisfied,risk_status,final_status,reason_codes_json,risk_json,"
            "sizing_json,decision_input_hash,observed_at,recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_key,
                PORTFOLIO_RUN_ID,
                security_id,
                from_state,
                to_state,
                to_state,
                SNAP_ID,
                "fixture-policy-v1",
                0,
                "INELIGIBLE",
                0,
                0,
                0,
                1,
                "NOT_RUN",
                "TRANSITIONED",
                "[]",
                "{}",
                "{}",
                _pad("obs-hash", 64),
                effective_at,
                effective_at,
            ),
        )
        conn.execute(
            "INSERT INTO portfolio_state_transition("
            "transition_id,decision_id,security_id,from_state,to_state,cause,reason_codes_json,"
            "score_snapshot_id,portfolio_snapshot_id,policy_version,persistence_count,"
            "persistence_required,effective_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _pad(transition_id),
                decision_key,
                security_id,
                from_state,
                to_state,
                "RULE_PERSISTED",
                "[]",
                SCORE_ID,
                SNAP_ID,
                "fixture-policy-v1",
                0,
                0,
                effective_at,
                effective_at,
            ),
        )
        return decision_key, _pad(transition_id)


def test_default_state_is_discover(tmp_path):
    db = _db(tmp_path)
    with db.connect(read_only=True) as conn:
        state = current_state(conn, "sec1", "2025-06-01T00:00:00Z")
    assert state["state"] == State.DISCOVER
    assert state["transition_id"] is None


def test_current_state_is_latest_at_or_before_as_of(tmp_path):
    db = _db(tmp_path)
    _insert_transition(db, to_state="WATCH", effective_at="2025-06-01T00:00:00Z")
    _insert_transition(
        db,
        from_state="WATCH",
        to_state="ENTER",
        effective_at="2025-06-03T00:00:00Z",
        decision_id="d1",
        transition_id="t1",
    )
    with db.connect(read_only=True) as conn:
        assert current_state(conn, "sec1", "2025-06-02T00:00:00Z")["state"] == State.WATCH
        assert current_state(conn, "sec1", "2025-06-03T00:00:00Z")["state"] == State.ENTER
        assert current_state(conn, "sec1", "2025-06-04T00:00:00Z")["state"] == State.ENTER
        assert current_state(conn, "sec1", "2025-05-01T00:00:00Z")["state"] == State.DISCOVER


def test_cooldown_inclusive_boundary():
    assert cooldown_satisfied("2025-06-01T00:00:00Z", "2025-06-03T00:00:00Z", 2) is True
    assert cooldown_satisfied("2025-06-01T00:00:00Z", "2025-06-03T00:00:00Z", 3) is False
    assert cooldown_satisfied("2025-06-01T00:00:00Z", "2025-06-02T23:59:59Z", 2) is False
    assert cooldown_satisfied(None, "2025-06-01T00:00:00Z", 5) is True
    assert cooldown_satisfied("2025-06-01T00:00:00Z", "2025-06-01T00:00:00Z", 0) is True


def test_persistence_counts_consecutive_evidence_driven_observations(tmp_path):
    db = _db(tmp_path)
    _insert_transition(db, to_state="WATCH", effective_at="2025-06-01T00:00:00Z")
    _insert_observation(db, as_of="2025-06-03T00:00:00Z", decision_id="d1", evidence_hash="H1")
    with db.connect(read_only=True) as conn:
        count = persistence_count(
            conn,
            "sec1",
            "fixture-policy-v1",
            "2025-06-05T00:00:00Z",
            State.WATCH,
            State.ENTER,
            "d-now",
            "H2",
            "PASS",
            State.ENTER,
        )
    assert count == 2  # prior H1 + hypothetical current H2


def test_unchanged_evidence_hash_does_not_increment(tmp_path):
    db = _db(tmp_path)
    _insert_transition(db, to_state="WATCH", effective_at="2025-06-01T00:00:00Z")
    _insert_observation(db, as_of="2025-06-03T00:00:00Z", decision_id="d1", evidence_hash="H1")
    with db.connect(read_only=True) as conn:
        count = persistence_count(
            conn,
            "sec1",
            "fixture-policy-v1",
            "2025-06-05T00:00:00Z",
            State.WATCH,
            State.ENTER,
            "d-now",
            "H1",
            "PASS",
            State.ENTER,
        )
    assert count == 1  # H1 deduped: only the hypothetical observation counts


def test_non_evidence_driven_observations_do_not_count(tmp_path):
    db = _db(tmp_path)
    _insert_transition(db, to_state="WATCH", effective_at="2025-06-01T00:00:00Z")
    _insert_observation(db, as_of="2025-06-03T00:00:00Z", decision_id="d1", evidence_hash="H1")
    _insert_observation(
        db, as_of="2025-06-04T00:00:00Z", decision_id="d2", evidence_hash="H2", evidence_driven=0
    )
    with db.connect(read_only=True) as conn:
        count = persistence_count(
            conn,
            "sec1",
            "fixture-policy-v1",
            "2025-06-05T00:00:00Z",
            State.WATCH,
            State.ENTER,
            "d-now",
            "H3",
            "PASS",
            State.ENTER,
        )
    assert count == 2  # H1 + current H3; the non-evidence H2 is excluded entirely


def test_persistence_resets_on_state_epoch(tmp_path):
    db = _db(tmp_path)
    _insert_transition(db, to_state="WATCH", effective_at="2025-06-01T00:00:00Z")
    _insert_observation(db, as_of="2025-06-03T00:00:00Z", decision_id="d1", evidence_hash="H1")
    _insert_transition(
        db,
        from_state="WATCH",
        to_state="ENTER",
        effective_at="2025-06-04T00:00:00Z",
        decision_id="d2",
        transition_id="t2",
    )
    with db.connect(read_only=True) as conn:
        # observations before the ENTER epoch must not count toward ENTER-state persistence
        count = persistence_count(
            conn,
            "sec1",
            "fixture-policy-v1",
            "2025-06-05T00:00:00Z",
            State.ENTER,
            State.HOLD,
            "d-now",
            "H2",
            "PASS",
            State.HOLD,
        )
    assert count == 1  # only the hypothetical current observation


def test_signal_status_break_resets_streak(tmp_path):
    db = _db(tmp_path)
    _insert_transition(db, to_state="WATCH", effective_at="2025-06-01T00:00:00Z")
    _insert_observation(db, as_of="2025-06-03T00:00:00Z", decision_id="d1", evidence_hash="H1")
    _insert_observation(
        db,
        as_of="2025-06-04T00:00:00Z",
        decision_id="d2",
        evidence_hash="H2",
        signal_status="INELIGIBLE",
        signal_state="DISCOVER",
    )
    with db.connect(read_only=True) as conn:
        count = persistence_count(
            conn,
            "sec1",
            "fixture-policy-v1",
            "2025-06-05T00:00:00Z",
            State.WATCH,
            State.ENTER,
            "d-now",
            "H3",
            "PASS",
            State.ENTER,
        )
    assert count == 1  # the INELIGIBLE observation broke the streak


def test_pending_resolution_enter_fill_and_stale(tmp_path):
    db = _db(tmp_path)
    # ENTER pending with originating proposal
    decision_key, transition_key = _insert_transition(
        db,
        from_state="WATCH",
        to_state="ENTER",
        effective_at="2025-06-01T00:00:00Z",
        decision_id="d1",
        transition_id="t1",
    )
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_activity_day(activity_date,policy_version,"
            "max_actionable_count,max_notional_microusd,input_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("2025-06-01", "fixture-policy-v1", 3, 5000000000, "i" * 64, "2025-06-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO trade_proposal(proposal_id,decision_id,transition_id,activity_date,"
            "security_id,current_state,proposed_state,action,reason_codes_json,conviction_ppm,"
            "data_quality_ppm,agreement_ppm,trajectory,current_weight_ppm,target_weight_ppm,"
            "max_quantity_microunits,completion_quantity_microunits,max_notional_microusd,"
            "order_constraints_json,score_snapshot_id,portfolio_snapshot_id,policy_version,"
            "sizing_policy_version,proposal_mode,requires_human_approval,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _pad("p1"),
                decision_key,
                transition_key,
                "2025-06-01",
                "sec1",
                "WATCH",
                "ENTER",
                "BUY",
                '["score_band"]',
                800000,
                900000,
                800000,
                "RISING",
                0,
                80000,
                1000000,
                101000000,
                800000000,
                '{"paper_only":true,"long_only":true,"limit_only":true,'
                '"quantity_increment_microunits":1000000}',
                SCORE_ID,
                SNAP_ID,
                "fixture-policy-v1",
                "fixture-sizing-v1",
                "PAPER",
                1,
                "2025-06-01T00:00:00Z",
            ),
        )
    with db.connect(read_only=True) as conn:
        current = current_state(conn, "sec1", "2025-06-10T00:00:00Z")
        # filled: trusted quantity reaches completion
        outcome, satisfied, _ = pending_resolution(
            conn, "sec1", current, 101_000_000, "2025-06-10T00:00:00Z", 1000, 30
        )
        assert outcome == "SETTLE_HOLD" and satisfied
        # not filled: stays pending
        outcome, satisfied, _ = pending_resolution(
            conn, "sec1", current, 1_000_000, "2025-06-10T00:00:00Z", 1000, 30
        )
        assert outcome == "STILL_PENDING" and not satisfied
        # stale: no fill after pending_max_calendar_days
        outcome, satisfied, reason = pending_resolution(
            conn, "sec1", current, 1_000_000, "2025-07-15T00:00:00Z", 1000, 30
        )
        assert outcome == "SETTLE_HOLD" and reason == "pending_stale"


def test_pending_exit_settles_to_watch_on_zero_quantity(tmp_path):
    db = _db(tmp_path)
    decision_key, transition_key = _insert_transition(
        db,
        from_state="HOLD",
        to_state="EXIT",
        effective_at="2025-06-01T00:00:00Z",
        decision_id="d1",
        transition_id="t1",
    )
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO portfolio_activity_day(activity_date,policy_version,"
            "max_actionable_count,max_notional_microusd,input_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("2025-06-01", "fixture-policy-v1", 3, 5000000000, "i" * 64, "2025-06-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO trade_proposal(proposal_id,decision_id,transition_id,activity_date,"
            "security_id,current_state,proposed_state,action,reason_codes_json,conviction_ppm,"
            "data_quality_ppm,agreement_ppm,trajectory,current_weight_ppm,target_weight_ppm,"
            "max_quantity_microunits,completion_quantity_microunits,max_notional_microusd,"
            "order_constraints_json,score_snapshot_id,portfolio_snapshot_id,policy_version,"
            "sizing_policy_version,proposal_mode,requires_human_approval,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _pad("p1"),
                decision_key,
                transition_key,
                "2025-06-01",
                "sec1",
                "HOLD",
                "EXIT",
                "SELL",
                '["risk_reduction"]',
                200000,
                900000,
                800000,
                "FALLING",
                50000,
                0,
                1000000,
                0,
                800000000,
                '{"paper_only":true,"long_only":true,"limit_only":true,'
                '"quantity_increment_microunits":1000000}',
                SCORE_ID,
                SNAP_ID,
                "fixture-policy-v1",
                "fixture-sizing-v1",
                "PAPER",
                1,
                "2025-06-01T00:00:00Z",
            ),
        )
    with db.connect(read_only=True) as conn:
        current = current_state(conn, "sec1", "2025-06-05T00:00:00Z")
        outcome, satisfied, _ = pending_resolution(
            conn, "sec1", current, 0, "2025-06-05T00:00:00Z", 1000, 30
        )
        assert outcome == "SETTLE_WATCH" and satisfied

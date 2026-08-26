"""Daily aggregate budget: day binding, restart safety, admission, cash."""

from __future__ import annotations

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.budget import Budget, BudgetState, admit_drafts
from tradehub_research.portfolio.fixtures import fixture_policy
from tradehub_research.portfolio.policy import PolicyRegistry
from tradehub_research.portfolio.types import Action


def _db(tmp_path) -> ResearchDB:
    database = ResearchDB(tmp_path / "budget.db")
    database.migrate()
    PolicyRegistry(database).register(fixture_policy())
    return database


def _draft(security_id, notional, action=Action.BUY.value, reason="score_band"):
    return {
        "security_id": security_id,
        "max_notional_microusd": notional,
        "action": action,
        "reason_codes": [reason],
        "category": "score_band" if reason == "score_band" else "verified_break",
    }


def test_day_binding_first_writer_wins(tmp_path):
    db = _db(tmp_path)
    budget = Budget(db)
    state = budget.bind_day("2025-06-01", fixture_policy())
    assert state.max_actionable_count == 3
    assert state.max_notional_microusd == 5_000_000_000
    # same day, same policy: returns stored caps
    again = budget.bind_day("2025-06-01", fixture_policy())
    assert again.max_actionable_count == 3


def test_day_policy_mismatch_fails_closed(tmp_path):
    from tradehub_research.portfolio.policy import build_policy
    from tradehub_research.portfolio.types import PolicyStatus

    db = _db(tmp_path)
    budget = Budget(db)
    budget.bind_day("2025-06-01", fixture_policy())
    spec = fixture_policy().as_dict()
    spec["budget"]["max_actionable_count"] = 1
    other = build_policy("other-v1", PolicyStatus.FIXTURE, spec)
    PolicyRegistry(db).register(other)
    with pytest.raises(ValueError, match="already bound"):
        budget.bind_day("2025-06-01", other)


def test_usage_derived_from_ledger_survives_restart(tmp_path):
    from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security

    db = _db(tmp_path)
    # simulate a PREVIOUS run having bound the day and consumed budget
    with db.connect() as conn:
        seed_security(conn, "sec1", sector="Tech")
        seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        score_id = seed_score(
            conn, pipeline_run_id="run1", security_id="sec1", committee_suffix="a"
        )
        conn.execute(
            "INSERT INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,cash_status,"
            "nav_microusd,valuation_status,holdings_status,provenance_json,input_hash,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "e" * 64,
                "2025-06-01T00:00:00Z",
                "USD",
                10_000_000_000,
                "KNOWN",
                10_000_000_000,
                "KNOWN",
                "KNOWN",
                "{}",
                "f" * 64,
                "2025-06-01T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO portfolio_run(run_id,pipeline_run_id,decision_as_of,portfolio_snapshot_id,"
            "policy_version,score_set_hash,signal_set_hash,candidate_set_hash,invocation_key,"
            "state_prestate_hash,market_data_prestate_hash,budget_prestate_hash,input_hash,"
            "expected_security_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "g" * 64,
                "run1",
                "2025-06-01T00:00:00Z",
                "e" * 64,
                "fixture-policy-v1",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "h" * 64,
                "i" * 64,
                "j" * 64,
                "k" * 64,
                1,
                "2025-06-01T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO portfolio_state_observation("
            "decision_id,run_id,security_id,current_state,signal_state,proposed_state,"
            "portfolio_snapshot_id,policy_version,evidence_driven,signal_status,"
            "persistence_count_at_decision,persistence_required,material_change_satisfied,"
            "cooldown_satisfied,risk_status,final_status,reason_codes_json,risk_json,"
            "sizing_json,decision_input_hash,observed_at,recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "b" * 64,
                "g" * 64,
                "sec1",
                "WATCH",
                "ENTER",
                "ENTER",
                "e" * 64,
                "fixture-policy-v1",
                1,
                "PASS",
                2,
                2,
                0,
                1,
                "PASS",
                "PROPOSED",
                '["score_band"]',
                "{}",
                "{}",
                "l" * 64,
                "2025-06-01T00:00:00Z",
                "2025-06-01T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO portfolio_state_transition("
            "transition_id,decision_id,security_id,from_state,to_state,cause,reason_codes_json,"
            "score_snapshot_id,portfolio_snapshot_id,policy_version,persistence_count,"
            "persistence_required,effective_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c" * 64,
                "b" * 64,
                "sec1",
                "WATCH",
                "ENTER",
                "RULE_PERSISTED",
                '["score_band"]',
                score_id,
                "e" * 64,
                "fixture-policy-v1",
                2,
                2,
                "2025-06-01T00:00:00Z",
                "2025-06-01T00:00:00Z",
            ),
        )
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
                "a" * 64,
                "b" * 64,
                "c" * 64,
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
                2_000_000_000,
                '{"paper_only":true,"long_only":true,"limit_only":true,'
                '"quantity_increment_microunits":1000000}',
                score_id,
                "e" * 64,
                "fixture-policy-v1",
                "fixture-sizing-v1",
                "PAPER",
                1,
                "2025-06-01T00:00:00Z",
            ),
        )
    # fresh Budget instance = "process restart"
    state = Budget(db).bind_day("2025-06-01", fixture_policy())
    assert state.used_count == 1
    assert state.used_notional_microusd == 2_000_000_000


def test_admission_count_cap():
    policy = fixture_policy()
    state = BudgetState("2025-06-01", "fixture-policy-v1", 2, 5_000_000_000, 0, 0)
    drafts = [
        _draft("sec1", 1_000_000_000),
        _draft("sec2", 1_000_000_000),
        _draft("sec3", 1_000_000_000),
    ]
    admitted, rejected = admit_drafts(state, drafts, policy, starting_cash_microusd=10_000_000_000)
    assert len(admitted) == 2
    assert rejected["sec3"] == "daily_budget_exhausted"


def test_admission_notional_cap():
    policy = fixture_policy()
    state = BudgetState("2025-06-01", "fixture-policy-v1", 5, 1_500_000_000, 0, 0)
    drafts = [_draft("sec1", 1_000_000_000), _draft("sec2", 1_000_000_000)]
    admitted, rejected = admit_drafts(state, drafts, policy, starting_cash_microusd=10_000_000_000)
    assert len(admitted) == 1
    assert rejected["sec2"] == "daily_budget_exhausted"


def test_many_small_drafts_cannot_bypass_count_cap():
    policy = fixture_policy()
    state = BudgetState("2025-06-01", "fixture-policy-v1", 3, 5_000_000_000, 0, 0)
    drafts = [_draft(f"sec{i}", 1_000_000) for i in range(10)]  # ten tiny proposals
    admitted, rejected = admit_drafts(state, drafts, policy, starting_cash_microusd=10_000_000_000)
    assert len(admitted) == 3
    assert len(rejected) == 7


def test_cash_accumulator_blocks_second_buy():
    policy = fixture_policy()
    state = BudgetState("2025-06-01", "fixture-policy-v1", 5, 20_000_000_000, 0, 0)
    drafts = [_draft("sec1", 6_000_000_000), _draft("sec2", 6_000_000_000)]
    admitted, rejected = admit_drafts(state, drafts, policy, starting_cash_microusd=8_000_000_000)
    assert len(admitted) == 1
    assert admitted[0]["security_id"] == "sec1"
    assert rejected["sec2"] == "cash_insufficient"


def test_deterministic_priority_order():
    policy = fixture_policy()
    state = BudgetState("2025-06-01", "fixture-policy-v1", 1, 5_000_000_000, 0, 0)
    verified = _draft("secA", 1_000_000_000, reason="thesis_broken")
    verified["category"] = "verified_break"
    score = _draft("secB", 1_000_000_000, reason="score_band")
    # order-independent: verified break wins regardless of input order
    admitted_a, _ = admit_drafts(
        state, [score, verified], policy, starting_cash_microusd=10_000_000_000
    )
    state2 = BudgetState("2025-06-01", "fixture-policy-v1", 1, 5_000_000_000, 0, 0)
    admitted_b, _ = admit_drafts(
        state2, [verified, score], policy, starting_cash_microusd=10_000_000_000
    )
    assert [d["security_id"] for d in admitted_a] == ["secA"]
    assert [d["security_id"] for d in admitted_b] == ["secA"]

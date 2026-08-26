"""Trade proposal builder: typed contract, SELL asymmetry, long-only invariants."""

from __future__ import annotations

import pytest

from tradehub_research.portfolio.proposal import ProposalError, build_proposal, proposal_id_for
from tradehub_research.portfolio.types import Action, State


def _kwargs(**overrides):
    base = dict(
        decision_id="d" * 64,
        transition_id="t" * 64,
        activity_date="2025-06-01",
        security_id="sec1",
        current_state=State.WATCH,
        proposed_state=State.ENTER,
        action=Action.BUY,
        reason_codes=["score_band"],
        conviction_ppm=800000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        target_weight_ppm=80000,
        max_quantity_microunits=1_000_000,
        completion_quantity_microunits=1_000_000,
        max_notional_microusd=800_000_000,
        score_snapshot_id="s" * 64,
        portfolio_snapshot_id="p" * 64,
        policy_version="fixture-policy-v1",
        sizing_policy_version="fixture-sizing-v1",
        quantity_increment_microunits=1_000_000,
        limit_only=True,
        created_at="2025-06-01T00:00:00Z",
    )
    base.update(overrides)
    return base


def test_buy_proposal_is_typed_and_immutable_shape():
    proposal = build_proposal(**_kwargs())
    assert proposal["proposal_mode"] == "PAPER"
    assert proposal["requires_human_approval"] == 1
    assert proposal["order_constraints_json"] == (
        '{"limit_only":true,"long_only":true,"paper_only":true,'
        '"quantity_increment_microunits":1000000}'
    )
    assert proposal["max_notional_microusd"] > 0
    assert proposal["max_quantity_microunits"] > 0


def test_proposal_id_is_deterministic():
    a = build_proposal(**_kwargs())
    b = build_proposal(**_kwargs())
    assert a["proposal_id"] == b["proposal_id"]
    assert proposal_id_for("d" * 64) == a["proposal_id"]


def test_buy_must_increase_target():
    with pytest.raises(ProposalError):
        build_proposal(**_kwargs(target_weight_ppm=0))


def test_buy_edge_constraint():
    # HOLD->ENTER is not a legal BUY edge (only WATCH->ENTER / HOLD->ADD)
    with pytest.raises(ProposalError):
        build_proposal(
            **_kwargs(current_state=State.HOLD, proposed_state=State.ENTER, action=Action.BUY)
        )


def test_sell_requires_asymmetric_reason():
    with pytest.raises(ProposalError, match="invalid SELL reason"):
        build_proposal(
            **_kwargs(
                current_state=State.HOLD,
                proposed_state=State.TRIM,
                action=Action.SELL,
                reason_codes=["falling_price"],
                current_weight_ppm=80000,
                target_weight_ppm=40000,
            )
        )


def test_sell_whitelisted_reasons():
    for reason in (
        "thesis_broken",
        "thesis_realised",
        "opportunity_cost",
        "risk_reduction",
        "data_integrity",
        "policy_ineligible",
    ):
        proposal = build_proposal(
            **_kwargs(
                current_state=State.HOLD,
                proposed_state=State.TRIM,
                action=Action.SELL,
                reason_codes=[reason],
                current_weight_ppm=80000,
                target_weight_ppm=40000,
            )
        )
        assert proposal["action"] == "SELL"


def test_sell_must_reduce_target():
    with pytest.raises(ProposalError):
        build_proposal(
            **_kwargs(
                current_state=State.HOLD,
                proposed_state=State.TRIM,
                action=Action.SELL,
                reason_codes=["risk_reduction"],
                current_weight_ppm=80000,
                target_weight_ppm=90000,
            )
        )


def test_sell_edge_constraint():
    with pytest.raises(ProposalError):
        build_proposal(
            **_kwargs(
                current_state=State.WATCH,
                proposed_state=State.ENTER,
                action=Action.SELL,
                reason_codes=["risk_reduction"],
                current_weight_ppm=80000,
                target_weight_ppm=0,
            )
        )


def test_reason_codes_required():
    with pytest.raises(ProposalError, match="at least one reason code"):
        build_proposal(**_kwargs(reason_codes=[]))


def test_zero_quantity_rejected():
    with pytest.raises(ProposalError):
        build_proposal(**_kwargs(max_quantity_microunits=0))

"""Typed immutable ``trade_proposal`` builder.

Proposals are PAPER recommendations / state artifacts only — never orders.
Identity is deterministic: ``proposal_id = D('trade-proposal-v1', decision_id)``
so retrying the same decision never creates a duplicate actionable proposal.
Long-only invariants are enforced here AND in the schema CHECK/trigger.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.portfolio.types import (
    SELL_REASONS,
    Action,
    D,
    State,
    json_roundtrip,
)

PROPOSAL_TAG = "trade-proposal-v1"


class ProposalError(ValueError):
    pass


def proposal_id_for(decision_id: str) -> str:
    return D(PROPOSAL_TAG, decision_id)


def build_proposal(
    *,
    decision_id: str,
    transition_id: str,
    activity_date: str,
    security_id: str,
    current_state: State,
    proposed_state: State,
    action: Action,
    reason_codes: list[str],
    conviction_ppm: int,
    data_quality_ppm: int,
    agreement_ppm: int,
    trajectory: str,
    current_weight_ppm: int,
    target_weight_ppm: int,
    max_quantity_microunits: int,
    completion_quantity_microunits: int,
    max_notional_microusd: int,
    score_snapshot_id: str,
    portfolio_snapshot_id: str,
    policy_version: str,
    sizing_policy_version: str,
    quantity_increment_microunits: int,
    limit_only: bool,
    created_at: str,
) -> dict[str, Any]:
    """Build a validated trade_proposal row dict (no DB writes)."""
    if not reason_codes:
        raise ProposalError("proposal requires at least one reason code")
    if action == Action.BUY:
        if not (
            (current_state == State.WATCH and proposed_state == State.ENTER)
            or (current_state == State.HOLD and proposed_state == State.ADD)
        ):
            raise ProposalError(
                f"BUY proposal requires WATCH->ENTER or HOLD->ADD, got "
                f"{current_state.value}->{proposed_state.value}"
            )
        if target_weight_ppm <= current_weight_ppm:
            raise ProposalError("BUY proposal must increase target weight")
    elif action == Action.SELL:
        if not (
            (current_state == State.HOLD and proposed_state in (State.TRIM, State.EXIT))
            or (current_state == State.TRIM and proposed_state == State.EXIT)
        ):
            raise ProposalError(
                f"SELL proposal requires HOLD->TRIM/EXIT or TRIM->EXIT, got "
                f"{current_state.value}->{proposed_state.value}"
            )
        if target_weight_ppm >= current_weight_ppm:
            raise ProposalError("SELL proposal must reduce target weight")
        invalid = [reason for reason in reason_codes if reason not in SELL_REASONS]
        if invalid:
            raise ProposalError(f"invalid SELL reason codes: {invalid}")
    else:
        raise ProposalError(f"unknown action {action!r}")
    if not (0 <= conviction_ppm <= 1_000_000):
        raise ProposalError("conviction_ppm out of range")
    if not (0 <= data_quality_ppm <= 1_000_000):
        raise ProposalError("data_quality_ppm out of range")
    if not (0 <= agreement_ppm <= 1_000_000):
        raise ProposalError("agreement_ppm out of range")
    if max_quantity_microunits <= 0:
        raise ProposalError("max_quantity_microunits must be positive")
    if completion_quantity_microunits < 0:
        raise ProposalError("completion_quantity_microunits cannot be negative")
    if max_notional_microusd <= 0:
        raise ProposalError("max_notional_microusd must be positive")
    order_constraints = {
        "paper_only": True,
        "long_only": True,
        "limit_only": bool(limit_only),
        "quantity_increment_microunits": quantity_increment_microunits,
    }
    proposal_id = proposal_id_for(decision_id)
    return {
        "proposal_id": proposal_id,
        "decision_id": decision_id,
        "transition_id": transition_id,
        "activity_date": activity_date,
        "security_id": security_id,
        "current_state": current_state.value,
        "proposed_state": proposed_state.value,
        "action": action.value,
        "reason_codes_json": json_roundtrip(sorted(reason_codes)),
        "conviction_ppm": conviction_ppm,
        "data_quality_ppm": data_quality_ppm,
        "agreement_ppm": agreement_ppm,
        "trajectory": trajectory,
        "current_weight_ppm": current_weight_ppm,
        "target_weight_ppm": target_weight_ppm,
        "max_quantity_microunits": max_quantity_microunits,
        "completion_quantity_microunits": completion_quantity_microunits,
        "max_notional_microusd": max_notional_microusd,
        "order_constraints_json": json_roundtrip(order_constraints),
        "score_snapshot_id": score_snapshot_id,
        "portfolio_snapshot_id": portfolio_snapshot_id,
        "policy_version": policy_version,
        "sizing_policy_version": sizing_policy_version,
        "proposal_mode": "PAPER",
        "requires_human_approval": 1,
        "created_at": created_at,
    }

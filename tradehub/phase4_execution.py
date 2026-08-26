from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tradehub_research.portfolio.execution import (
    PreviewIntent,
    SanitizedSettlement,
    apply_fill_to_portfolio,
    sanitize_settlement,
)


class ApprovalRequired(ValueError):
    pass


@dataclass(frozen=True)
class ApprovalContext:
    proposal_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float
    currency: str
    current_state: str
    proposed_state: str
    rationale: str
    score_snapshot_id: str


@dataclass(frozen=True)
class ExecutionResult:
    proposal_id: str
    execution_ref: str
    settlement: SanitizedSettlement
    portfolio: Any


class Phase4ExecutionBoundary:
    """Execution-side coordinator; broker callbacks never cross into research code."""

    def __init__(
        self,
        *,
        preview: Callable[[PreviewIntent], Mapping[str, Any]],
        submit: Callable[[str], str],
        reconcile: Callable[[str], Mapping[str, Any] | None],
        prove_paper: Callable[[], bool],
    ) -> None:
        self._preview = preview
        self._submit = submit
        self._reconcile = reconcile
        self._prove_paper = prove_paper
        self._previewed: PreviewIntent | None = None
        self._confirmation_token: str | None = None
        self._broker_order_ref: str | None = None

    def preview(self, intent: PreviewIntent) -> Mapping[str, Any]:
        result = self._preview(intent)
        if result.get("accepted") is not True or not result.get("confirmation_token"):
            raise ApprovalRequired("broker preview was not accepted")
        self._previewed = intent
        self._confirmation_token = str(result["confirmation_token"])
        return {"accepted": True, "proposal_id": intent.proposal_id}

    def render_approval(
        self, intent: PreviewIntent, *, current_state: str, proposed_state: str, rationale: str
    ) -> ApprovalContext:
        if self._previewed != intent:
            raise ApprovalRequired("approval must follow the exact current preview")
        return ApprovalContext(
            proposal_id=intent.proposal_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            currency=intent.currency,
            current_state=current_state,
            proposed_state=proposed_state,
            rationale=rationale,
            score_snapshot_id=intent.score_snapshot_id,
        )

    def affirm(self, context: ApprovalContext, *, exact_order: ApprovalContext) -> None:
        if context != exact_order or self._previewed is None:
            raise ApprovalRequired("affirmation does not match the exact rendered order")
        if not self._prove_paper():
            raise ApprovalRequired("broker account is not positively proven PAPER")
        # The raw token is deliberately retained only in this execution object.
        self._broker_order_ref = self._submit(self._confirmation_token)

    def reconcile_and_settle(
        self, *, current_quantity: float, action: str, proposed_state: str
    ) -> ExecutionResult:
        if (
            self._previewed is None
            or self._confirmation_token is None
            or self._broker_order_ref is None
        ):
            raise ApprovalRequired("explicit approval is required before reconciliation")
        proposal = self._previewed
        broker_order = self._reconcile(self._broker_order_ref)
        settlement = sanitize_settlement(
            proposal_id=proposal.proposal_id,
            execution_ref=proposal.proposal_id,
            order=broker_order,
            requested_qty=proposal.quantity,
        )
        portfolio = apply_fill_to_portfolio(
            proposal_id=proposal.proposal_id,
            execution_ref=proposal.proposal_id,
            action=action,
            proposed_state=proposed_state,
            current_quantity=current_quantity,
            settlement=settlement,
        )
        return ExecutionResult(proposal.proposal_id, proposal.proposal_id, settlement, portfolio)

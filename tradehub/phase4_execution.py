from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tradehub_research.portfolio.execution import (
    PreviewIntent,
    SanitizedSettlement,
    SettlementState,
    apply_fill_to_portfolio,
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


def _sanitize_broker_order(
    *,
    proposal_id: str,
    execution_ref: str,
    order: Mapping[str, Any] | None,
    requested_qty: float,
) -> SanitizedSettlement:
    if not order:
        state = SettlementState.INDETERMINATE
    else:
        status = str(order.get("status", "")).upper()
        filled = float(order.get("filled") or order.get("filled_quantity") or 0)
        if status in {"REJECTED", "FAILED"}:
            state = SettlementState.REJECTED
        elif status == "EXPIRED":
            state = SettlementState.EXPIRED
        elif status in {"CANCELLED", "CANCELED"}:
            state = SettlementState.CANCELLED
        elif filled >= requested_qty and requested_qty > 0:
            state = SettlementState.FILLED
        elif filled > 0:
            state = SettlementState.PARTIALLY_FILLED
        elif status in {"HELD", "SUBMITTED", "PENDING", "OPEN", "PARTIALLY_FILLED"}:
            state = SettlementState.OPEN
        else:
            state = SettlementState.INDETERMINATE
    filled = float((order or {}).get("filled") or (order or {}).get("filled_quantity") or 0)
    filled = max(0.0, min(filled, requested_qty))
    return SanitizedSettlement(
        proposal_id=proposal_id,
        execution_ref=execution_ref,
        broker_order_ref=(None if not order else str(order.get("id") or order.get("order_id"))),
        state=state,
        requested_qty=requested_qty,
        filled_qty=filled,
        remaining_qty=max(0.0, requested_qty - filled),
        avg_fill_price=(
            None
            if not order or order.get("avg_fill_price") is None
            else float(order["avg_fill_price"])
        ),
        terminal=state
        in {
            SettlementState.FILLED,
            SettlementState.CANCELLED,
            SettlementState.EXPIRED,
            SettlementState.REJECTED,
        },
        reason="execution-side broker reconciliation",
    )


class Phase4ExecutionBoundary:
    """Execution-side coordinator; broker callbacks never cross into research code."""

    def __init__(
        self,
        *,
        preview: Callable[[PreviewIntent], Mapping[str, Any]],
        submit: Callable[[str], str],
        reconcile: Callable[[str], Mapping[str, Any] | None],
        prove_paper: Callable[[], bool],
        persist_execution_link: Callable[[str, str, Mapping[str, str]], None] | None = None,
    ) -> None:
        self._preview = preview
        self._submit = submit
        self._reconcile = reconcile
        self._prove_paper = prove_paper
        self._persist_execution_link = persist_execution_link or (
            lambda proposal_id, execution_ref, metadata: None
        )
        self._previewed: PreviewIntent | None = None
        self._confirmation_token: str | None = None
        self._broker_order_ref: str | None = None

    def preview(self, intent: PreviewIntent) -> Mapping[str, Any]:
        result = self._preview(intent)
        if result.get("accepted") is not True or not result.get("confirmation_token"):
            raise ApprovalRequired("broker preview was not accepted")
        self._previewed = intent
        self._confirmation_token = str(result["confirmation_token"])
        self._execution_ref = f"execution:{intent.proposal_id}"
        token_ref = hashlib.sha256(self._confirmation_token.encode()).hexdigest()
        self._persist_execution_link(
            intent.proposal_id,
            self._execution_ref,
            {"confirmation_token_ref": token_ref},
        )
        return {
            "accepted": True,
            "proposal_id": intent.proposal_id,
            "execution_ref": self._execution_ref,
        }

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
        self._persist_execution_link(
            self._previewed.proposal_id,
            self._execution_ref,
            {"broker_order_ref": str(self._broker_order_ref)},
        )

    def reconcile_and_settle(
        self,
        *,
        current_quantity: float,
        action: str,
        proposed_state: str,
        prior_state: str | None = None,
    ) -> ExecutionResult:
        if (
            self._previewed is None
            or self._confirmation_token is None
            or self._broker_order_ref is None
        ):
            raise ApprovalRequired("explicit approval is required before reconciliation")
        proposal = self._previewed
        broker_order = self._reconcile(self._broker_order_ref)
        broker_ref = (
            None if not broker_order else (broker_order.get("id") or broker_order.get("order_id"))
        )
        if str(broker_ref) != str(self._broker_order_ref):
            broker_order = None
        settlement = _sanitize_broker_order(
            proposal_id=proposal.proposal_id,
            execution_ref=self._execution_ref,
            order=broker_order,
            requested_qty=proposal.quantity,
        )
        portfolio = apply_fill_to_portfolio(
            proposal_id=proposal.proposal_id,
            execution_ref=self._execution_ref,
            action=action,
            proposed_state=proposed_state,
            current_quantity=current_quantity,
            settlement=settlement,
            prior_state=prior_state,
        )
        return ExecutionResult(proposal.proposal_id, self._execution_ref, settlement, portfolio)

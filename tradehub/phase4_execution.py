"""Execution-side Phase-4 coordinator.

A single ``Phase4ExecutionBoundary`` instance handles exactly ONE proposal's
lifecycle end to end (preview -> approval -> submit -> reconcile). It is
deliberately single-use: broker callbacks and secrets never cross into
research code, and no state from one proposal can bleed into another because
the instance refuses to preview a second proposal without an explicit
``reset()``.

Approval binding: the boundary itself renders and retains the canonical
``ApprovalContext`` produced from the CURRENT preview. ``affirm()`` compares
the caller's context against that retained canonical context -- a caller
cannot preview proposal A, render/display a different context B, and then
affirm(B) while consuming A's confirmation token, because there is only ever
one retained context and it is the boundary's own record of what was
rendered, not a value the caller can substitute.

Settlement direction: ``reconcile_and_settle`` takes NO caller-supplied
``action``/``proposed_state``/``prior_state``. Those come exclusively from
the approved ``ApprovalContext`` (``side``, ``proposed_state``,
``current_state``) -- an approved BUY can never be settled as SELL, and the
state transition being settled is always the one that was actually approved.
"""

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
    """Single-use, single-proposal execution-side coordinator."""

    def __init__(
        self,
        *,
        preview: Callable[[PreviewIntent], Mapping[str, Any]],
        submit: Callable[[str], str],
        reconcile: Callable[[str], Mapping[str, Any] | None],
        prove_paper: Callable[[], bool],
        persist_execution_link: Callable[[str, str, Mapping[str, str]], None] | None = None,
        already_applied_fill: float = 0.0,
    ) -> None:
        self._preview = preview
        self._submit = submit
        self._reconcile = reconcile
        self._prove_paper = prove_paper
        self._persist_execution_link = persist_execution_link or (
            lambda proposal_id, execution_ref, metadata: None
        )
        self._initial_applied_fill = already_applied_fill
        self._reset_proposal_state()

    def _reset_proposal_state(self) -> None:
        self._previewed: PreviewIntent | None = None
        self._confirmation_token: str | None = None
        self._execution_ref: str | None = None
        self._rendered_context: ApprovalContext | None = None
        self._broker_order_ref: str | None = None
        self._applied_fill: float = self._initial_applied_fill

    def reset(self) -> None:
        """Explicitly discard all proposal-scoped state.

        No broker order ref, rendered approval, affirmation, confirmation, or
        applied-fill state survives into the next proposal. Callers should
        prefer constructing a fresh boundary per proposal; this exists for
        long-lived orchestrators that reuse one instance across proposals.
        """
        self._reset_proposal_state()

    @classmethod
    def recover_previewed(
        cls,
        *,
        intent: PreviewIntent,
        confirmation_token: str,
        execution_ref: str,
        submit: Callable[[str], str],
        reconcile: Callable[[str], Mapping[str, Any] | None],
        prove_paper: Callable[[], bool],
        persist_execution_link: Callable[[str, str, Mapping[str, str]], None] | None = None,
        broker_order_ref: str | None = None,
        already_applied_fill: float = 0.0,
    ) -> Phase4ExecutionBoundary:
        """Restart-recovery constructor.

        Reconstructs a boundary already in the PREVIEWED (or later, if
        ``broker_order_ref`` is supplied) state WITHOUT re-invoking the
        broker preview endpoint. The caller is responsible for having
        recovered ``confirmation_token`` from execution-side authority
        (e.g. ``AuditStore.find_active_confirmation_by_client_request_id``)
        and verified its hash against the safe research-side reference
        before calling this. Approval must still be freshly rendered and
        affirmed -- recovery never skips explicit human affirmation.
        """
        boundary = cls(
            preview=lambda _intent: {"accepted": True, "confirmation_token": confirmation_token},
            submit=submit,
            reconcile=reconcile,
            prove_paper=prove_paper,
            persist_execution_link=persist_execution_link,
            already_applied_fill=already_applied_fill,
        )
        boundary._previewed = intent
        boundary._confirmation_token = confirmation_token
        boundary._execution_ref = execution_ref
        boundary._broker_order_ref = broker_order_ref
        return boundary

    def preview(self, intent: PreviewIntent) -> Mapping[str, Any]:
        if self._previewed is not None:
            raise ApprovalRequired(
                "boundary already has an active proposal in flight; call reset() "
                "or construct a fresh boundary before previewing another proposal"
            )
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
        context = ApprovalContext(
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
        # The boundary retains this as the ONE canonical rendered context.
        # affirm() compares against THIS retained value, never a caller-
        # supplied "exact_order" -- a fabricated context can never be
        # affirmed because it will not equal what render_approval produced.
        self._rendered_context = context
        return context

    def affirm(self, context: ApprovalContext) -> None:
        if self._previewed is None or self._rendered_context is None:
            raise ApprovalRequired("approval must be rendered before affirmation")
        if context != self._rendered_context:
            raise ApprovalRequired("affirmation does not match the canonical rendered order")
        if not self._prove_paper():
            raise ApprovalRequired("broker account is not positively proven PAPER")
        # The raw token is deliberately retained only in this execution object.
        self._broker_order_ref = self._submit(self._confirmation_token)
        self._persist_execution_link(
            self._previewed.proposal_id,
            self._execution_ref,
            {"broker_order_ref": str(self._broker_order_ref)},
        )

    def reconcile_and_settle(self, *, current_quantity: float) -> ExecutionResult:
        if (
            self._previewed is None
            or self._confirmation_token is None
            or self._broker_order_ref is None
            or self._rendered_context is None
        ):
            raise ApprovalRequired("explicit approval is required before reconciliation")
        proposal = self._previewed
        context = self._rendered_context
        # Direction and target state come EXCLUSIVELY from the approved
        # context -- never from a caller-supplied action/proposed_state.
        action = context.side
        proposed_state = context.proposed_state
        prior_state = context.current_state
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
            already_applied_fill=self._applied_fill,
        )
        self._applied_fill = portfolio.applied_fill
        self._persist_execution_link(
            proposal.proposal_id,
            self._execution_ref,
            {
                "settlement_state": settlement.state.value,
                "applied_fill": str(self._applied_fill),
                "owned_quantity": str(portfolio.owned_quantity),
            },
        )
        return ExecutionResult(proposal.proposal_id, self._execution_ref, settlement, portfolio)

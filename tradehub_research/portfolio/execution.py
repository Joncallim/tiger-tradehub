from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ProposalExecutionError(ValueError):
    pass


class SettlementState(str, Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    OPEN = "OPEN"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class PreviewIntent:
    proposal_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float
    currency: str
    score_snapshot_id: str
    portfolio_snapshot_id: str
    policy_version: str
    reason: str


@dataclass(frozen=True)
class SanitizedSettlement:
    proposal_id: str
    execution_ref: str
    broker_order_ref: str | None
    state: SettlementState
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    avg_fill_price: float | None
    terminal: bool
    reason: str


def proposal_to_preview_intent(
    proposal: Mapping[str, Any],
    *,
    allowlist: set[str],
    current_day_count: int,
    current_day_notional: float,
    max_day_count: int,
    max_day_notional: float,
) -> PreviewIntent:
    """Translate only immutable typed proposal fields into an execution intent."""
    required = (
        "proposal_id",
        "security_id",
        "action",
        "max_quantity_microunits",
        "max_notional_microusd",
        "score_snapshot_id",
        "portfolio_snapshot_id",
        "policy_version",
    )
    missing = [key for key in required if proposal.get(key) in (None, "")]
    if missing:
        raise ProposalExecutionError(f"proposal missing pinned fields: {missing}")
    if not allowlist:
        raise ProposalExecutionError("V2 execution allowlist is empty; refusing execution")
    symbol = str(
        proposal.get("canonical_ticker") or proposal.get("ticker") or proposal["security_id"]
    ).upper()
    if symbol not in {item.upper() for item in allowlist}:
        raise ProposalExecutionError(f"symbol {symbol!r} is not in the V2 execution allowlist")
    if current_day_count >= max_day_count:
        raise ProposalExecutionError("daily proposal count budget exhausted")
    notional = float(proposal["max_notional_microusd"]) / 1_000_000
    if current_day_notional + notional > max_day_notional:
        raise ProposalExecutionError("daily proposal notional budget exhausted")
    side = str(proposal["action"]).upper()
    if side not in {"BUY", "SELL"}:
        raise ProposalExecutionError("proposal action must be BUY or SELL")
    quantity = float(proposal["max_quantity_microunits"]) / 1_000_000
    price = float(proposal["max_notional_microusd"]) / 1_000_000 / quantity
    if quantity <= 0 or price <= 0:
        raise ProposalExecutionError("proposal quantity and limit price must be positive")
    return PreviewIntent(
        proposal_id=str(proposal["proposal_id"]),
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type="LIMIT",
        limit_price=price,
        currency="USD",
        score_snapshot_id=str(proposal["score_snapshot_id"]),
        portfolio_snapshot_id=str(proposal["portfolio_snapshot_id"]),
        policy_version=str(proposal["policy_version"]),
        reason="typed proposal execution; model prose excluded",
    )


def classify_settlement(
    order: Mapping[str, Any] | None,
    *,
    requested_qty: float,
) -> SettlementState:
    if not order:
        return SettlementState.INDETERMINATE
    status = str(order.get("status", "")).upper()
    filled = float(order.get("filled") or order.get("filled_quantity") or 0)
    if status in {"REJECTED", "FAILED"}:
        return SettlementState.REJECTED
    if status == "EXPIRED":
        return SettlementState.EXPIRED
    if status in {"CANCELLED", "CANCELED"}:
        return SettlementState.CANCELLED
    if filled >= requested_qty and requested_qty > 0:
        return SettlementState.FILLED
    if filled > 0:
        return SettlementState.PARTIALLY_FILLED
    if status in {"HELD", "SUBMITTED", "PENDING", "OPEN", "PARTIALLY_FILLED"}:
        return SettlementState.OPEN
    return SettlementState.INDETERMINATE


def sanitize_settlement(
    *,
    proposal_id: str,
    execution_ref: str,
    order: Mapping[str, Any] | None,
    requested_qty: float,
    reason: str = "broker reconciliation",
) -> SanitizedSettlement:
    state = classify_settlement(order, requested_qty=requested_qty)
    filled = float((order or {}).get("filled") or (order or {}).get("filled_quantity") or 0)
    filled = max(0.0, min(filled, requested_qty))
    remaining = max(0.0, requested_qty - filled)
    broker_ref = None if not order else str(order.get("id") or order.get("order_id"))
    avg = (
        None if not order or order.get("avg_fill_price") is None else float(order["avg_fill_price"])
    )
    terminal = state in {
        SettlementState.FILLED,
        SettlementState.CANCELLED,
        SettlementState.EXPIRED,
        SettlementState.REJECTED,
    }
    return SanitizedSettlement(
        proposal_id=proposal_id,
        execution_ref=execution_ref,
        broker_order_ref=broker_ref,
        state=state,
        requested_qty=requested_qty,
        filled_qty=filled,
        remaining_qty=remaining,
        avg_fill_price=avg,
        terminal=terminal,
        reason=reason,
    )


@dataclass(frozen=True)
class PortfolioSettlement:
    proposal_id: str
    execution_ref: str
    settlement: SanitizedSettlement
    portfolio_mutated: bool
    owned_quantity: float
    sold_quantity: float
    next_state: str


def apply_fill_to_portfolio(
    *,
    proposal_id: str,
    execution_ref: str,
    action: str,
    proposed_state: str,
    current_quantity: float,
    settlement: SanitizedSettlement,
    prior_state: str | None = None,
) -> PortfolioSettlement:
    """Derive portfolio settlement from actual broker fill evidence only."""
    if settlement.state == SettlementState.INDETERMINATE:
        return PortfolioSettlement(
            proposal_id,
            execution_ref,
            settlement,
            False,
            current_quantity,
            0.0,
            "PENDING_RECONCILIATION",
        )
    if action == "BUY":
        owned = current_quantity + settlement.filled_qty
        if settlement.filled_qty == 0 and settlement.terminal:
            return PortfolioSettlement(
                proposal_id,
                execution_ref,
                settlement,
                False,
                current_quantity,
                0.0,
                prior_state or proposed_state,
            )
        next_state = "HOLD" if settlement.filled_qty > 0 else proposed_state
        return PortfolioSettlement(
            proposal_id,
            execution_ref,
            settlement,
            settlement.filled_qty > 0,
            owned,
            0.0,
            next_state,
        )
    if action == "SELL":
        sold = min(current_quantity, settlement.filled_qty)
        remaining = max(0.0, current_quantity - sold)
        if settlement.filled_qty == 0 and settlement.state == SettlementState.OPEN:
            return PortfolioSettlement(
                proposal_id,
                execution_ref,
                settlement,
                False,
                current_quantity,
                0.0,
                proposed_state,
            )
        next_state = "WATCH" if remaining == 0 else "HOLD"
        return PortfolioSettlement(
            proposal_id,
            execution_ref,
            settlement,
            sold > 0,
            remaining,
            sold,
            next_state,
        )
    raise ProposalExecutionError("settlement action must be BUY or SELL")

import pytest

from tradehub_research.portfolio.execution import (
    ProposalExecutionError,
    SettlementState,
    apply_fill_to_portfolio,
    classify_settlement,
    proposal_to_preview_intent,
    sanitize_settlement,
)

PROPOSAL = {
    "proposal_id": "p1",
    "security_id": "AAPL",
    "action": "BUY",
    "max_quantity_microunits": 1_000_000,
    "max_notional_microusd": 150_000_000,
    "score_snapshot_id": "score1",
    "portfolio_snapshot_id": "portfolio1",
    "policy_version": "policy1",
}


def test_stable_security_id_uses_canonical_ticker():
    proposal = {**PROPOSAL, "security_id": "sec-123", "canonical_ticker": "AAPL"}
    intent = proposal_to_preview_intent(
        proposal,
        allowlist={"AAPL"},
        current_day_count=0,
        current_day_notional=0,
        max_day_count=3,
        max_day_notional=1000,
    )
    assert intent.symbol == "AAPL"


def test_proposal_translation_is_typed_and_deterministic():
    one = proposal_to_preview_intent(
        PROPOSAL,
        allowlist={"AAPL"},
        current_day_count=0,
        current_day_notional=0,
        max_day_count=3,
        max_day_notional=1000,
    )
    two = proposal_to_preview_intent(
        dict(PROPOSAL),
        allowlist={"AAPL"},
        current_day_count=0,
        current_day_notional=0,
        max_day_count=3,
        max_day_notional=1000,
    )
    assert one == two
    assert one.side == "BUY"
    assert one.order_type == "LIMIT"
    assert one.limit_price == 150
    assert "model prose" in one.reason


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"allowlist": set()}, "allowlist"),
        ({"allowlist": {"MSFT"}}, "allowlist"),
        ({"allowlist": {"AAPL"}, "current_day_count": 3}, "count"),
        ({"allowlist": {"AAPL"}, "current_day_notional": 900}, "notional"),
    ],
)
def test_translation_revalidates_execution_policy(kwargs, message):
    base = {
        "allowlist": {"AAPL"},
        "current_day_count": 0,
        "current_day_notional": 0,
        "max_day_count": 3,
        "max_day_notional": 1000,
    }
    base.update(kwargs)
    with pytest.raises(ProposalExecutionError, match=message):
        proposal_to_preview_intent(PROPOSAL, **base)


def test_translation_rejects_model_supplied_market_fields():
    proposal = {**PROPOSAL, "limit_price": 999, "prose": "buy 999 shares"}
    intent = proposal_to_preview_intent(
        proposal,
        allowlist={"AAPL"},
        current_day_count=0,
        current_day_notional=0,
        max_day_count=3,
        max_day_notional=1000,
    )
    assert intent.limit_price == 150
    assert intent.quantity == 1


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ({"status": "FILLED", "filled": 1}, SettlementState.FILLED),
        ({"status": "SUBMITTED", "filled": 0.4}, SettlementState.PARTIALLY_FILLED),
        ({"status": "SUBMITTED", "filled": 0}, SettlementState.OPEN),
        ({"status": "CANCELLED", "filled": 0}, SettlementState.CANCELLED),
        ({"status": "EXPIRED", "filled": 0}, SettlementState.EXPIRED),
        ({"status": "REJECTED", "filled": 0}, SettlementState.REJECTED),
        ({"status": "ALIEN", "filled": 0}, SettlementState.INDETERMINATE),
        (None, SettlementState.INDETERMINATE),
    ],
)
def test_settlement_uses_broker_evidence(order, expected):
    assert classify_settlement(order, requested_qty=1) == expected


def test_unfilled_enter_does_not_create_hold():
    settlement = sanitize_settlement(
        proposal_id="p1",
        execution_ref="opaque-ref",
        order={"id": "broker-1", "status": "SUBMITTED", "filled": 0},
        requested_qty=1,
    )
    result = apply_fill_to_portfolio(
        proposal_id="p1",
        execution_ref="opaque-ref",
        action="BUY",
        proposed_state="ENTER",
        current_quantity=0,
        settlement=settlement,
    )
    assert not result.portfolio_mutated
    assert result.owned_quantity == 0
    assert result.next_state == "ENTER"


def test_terminal_zero_fill_buy_restores_prior_state():
    settlement = sanitize_settlement(
        proposal_id="p5",
        execution_ref="opaque-ref",
        order={"status": "REJECTED", "filled": 0},
        requested_qty=1,
    )
    result = apply_fill_to_portfolio(
        proposal_id="p5",
        execution_ref="opaque-ref",
        action="BUY",
        proposed_state="ENTER",
        prior_state="WATCH",
        current_quantity=0,
        settlement=settlement,
    )
    assert not result.portfolio_mutated
    assert result.next_state == "WATCH"


def test_zero_fill_open_sell_remains_pending():
    settlement = sanitize_settlement(
        proposal_id="p4",
        execution_ref="opaque-ref",
        order={"status": "OPEN", "filled": 0},
        requested_qty=1,
    )
    result = apply_fill_to_portfolio(
        proposal_id="p4",
        execution_ref="opaque-ref",
        action="SELL",
        proposed_state="TRIM",
        current_quantity=1,
        settlement=settlement,
    )
    assert not result.portfolio_mutated
    assert result.owned_quantity == 1
    assert result.next_state == "TRIM"


def test_partial_sell_reduces_only_actual_position():
    settlement = sanitize_settlement(
        proposal_id="p2",
        execution_ref="opaque-ref",
        order={"id": "broker-2", "status": "SUBMITTED", "filled": 0.4},
        requested_qty=1,
    )
    result = apply_fill_to_portfolio(
        proposal_id="p2",
        execution_ref="opaque-ref",
        action="SELL",
        proposed_state="TRIM",
        current_quantity=1,
        settlement=settlement,
    )
    assert result.portfolio_mutated
    assert result.sold_quantity == 0.4
    assert result.owned_quantity == 0.6
    assert result.next_state == "HOLD"


def test_indeterminate_settlement_does_not_mutate_portfolio():
    settlement = sanitize_settlement(
        proposal_id="p3",
        execution_ref="opaque-ref",
        order=None,
        requested_qty=1,
    )
    result = apply_fill_to_portfolio(
        proposal_id="p3",
        execution_ref="opaque-ref",
        action="BUY",
        proposed_state="ENTER",
        current_quantity=0,
        settlement=settlement,
    )
    assert not result.portfolio_mutated
    assert result.next_state == "PENDING_RECONCILIATION"

    result = sanitize_settlement(
        proposal_id="p1",
        execution_ref="opaque-ref",
        order={"id": "broker-1", "status": "SUBMITTED", "filled": 0.4},
        requested_qty=1,
    )
    assert result.state == SettlementState.PARTIALLY_FILLED
    assert result.filled_qty == 0.4
    assert result.remaining_qty == 0.6
    assert result.broker_order_ref == "broker-1"
    assert not result.terminal

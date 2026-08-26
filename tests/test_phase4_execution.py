from tradehub.phase4_execution import ApprovalRequired, Phase4ExecutionBoundary
from tradehub_research.portfolio.execution import PreviewIntent, SettlementState

INTENT = PreviewIntent(
    proposal_id="p1",
    symbol="AAPL",
    side="BUY",
    quantity=1,
    order_type="LIMIT",
    limit_price=150,
    currency="USD",
    score_snapshot_id="score1",
    portfolio_snapshot_id="portfolio1",
    policy_version="policy1",
    reason="typed proposal execution; model prose excluded",
)


def test_execution_boundary_requires_exact_approval_and_paper_proof():
    calls = []
    boundary = Phase4ExecutionBoundary(
        preview=lambda intent: {"accepted": True, "confirmation_token": "raw-token"},
        submit=lambda token: calls.append(("submit", token)) or "broker-1",
        reconcile=lambda ref: {"id": ref, "status": "SUBMITTED", "filled": 0},
        prove_paper=lambda: True,
    )
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context, exact_order=context)
    result = boundary.reconcile_and_settle(current_quantity=0, action="BUY", proposed_state="ENTER")
    assert calls == [("submit", "raw-token")]
    assert result.settlement.state == SettlementState.OPEN
    assert not result.portfolio.portfolio_mutated
    assert "raw-token" not in repr(result)


def test_execution_boundary_blocks_nonpaper_before_submit():
    calls = []
    boundary = Phase4ExecutionBoundary(
        preview=lambda intent: {"accepted": True, "confirmation_token": "raw-token"},
        submit=lambda token: calls.append(token) or "broker-1",
        reconcile=lambda ref: None,
        prove_paper=lambda: False,
    )
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    try:
        boundary.affirm(context, exact_order=context)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("non-PAPER account was not blocked")
    assert calls == []

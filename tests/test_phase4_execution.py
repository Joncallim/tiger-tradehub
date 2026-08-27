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


def _boundary(**overrides):
    defaults = dict(
        preview=lambda intent: {"accepted": True, "confirmation_token": "raw-token"},
        submit=lambda token: "broker-1",
        reconcile=lambda ref: {"id": ref, "status": "SUBMITTED", "filled": 0},
        prove_paper=lambda: True,
    )
    defaults.update(overrides)
    return Phase4ExecutionBoundary(**defaults)


def test_execution_boundary_requires_exact_approval_and_paper_proof():
    calls = []
    boundary = _boundary(submit=lambda token: calls.append(("submit", token)) or "broker-1")
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    assert calls == [("submit", "raw-token")]
    assert result.settlement.state == SettlementState.OPEN
    assert not result.portfolio.portfolio_mutated
    assert "raw-token" not in repr(result)


def test_preview_persists_only_opaque_execution_reference():
    links = []
    boundary = _boundary(
        reconcile=lambda ref: {"id": ref, "status": "OPEN", "filled": 0},
        persist_execution_link=lambda proposal_id, execution_ref, metadata: links.append(
            (proposal_id, execution_ref, metadata)
        ),
    )
    result = boundary.preview(INTENT)
    assert result["execution_ref"] == "execution:p1"
    assert links[0][0:2] == ("p1", "execution:p1")
    assert "confirmation_token_ref" in links[0][2]
    assert "raw-token" not in repr(links)


def test_reconciliation_accepts_normalized_order_id():
    boundary = _boundary(reconcile=lambda ref: {"order_id": ref, "status": "FILLED", "filled": 1})
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="pinned reason"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    assert result.settlement.state == SettlementState.FILLED


def test_reconciliation_id_mismatch_is_indeterminate():
    boundary = _boundary(reconcile=lambda ref: {"id": "broker-2", "status": "FILLED", "filled": 1})
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="pinned reason"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    assert result.settlement.state == SettlementState.INDETERMINATE
    assert not result.portfolio.portfolio_mutated


def test_execution_boundary_blocks_nonpaper_before_submit():
    calls = []
    boundary = _boundary(
        submit=lambda token: calls.append(token) or "broker-1",
        reconcile=lambda ref: None,
        prove_paper=lambda: False,
    )
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    try:
        boundary.affirm(context)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("non-PAPER account was not blocked")
    assert calls == []


# --- A. Approval must be bound to the canonical rendered context ---


def test_affirm_rejects_fabricated_context_even_if_internally_consistent():
    """A caller cannot preview A, render/display fake context B, then
    affirm(B) while consuming A's confirmation token -- the boundary compares
    against its OWN retained canonical render, not a caller-supplied value."""
    calls = []
    boundary = _boundary(submit=lambda token: calls.append(token) or "broker-1")
    boundary.preview(INTENT)
    real_context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    # Fabricate a "benign" context that differs only in quantity -- never
    # produced by render_approval, so it must never match.
    fake_context = real_context.__class__(**{**real_context.__dict__, "quantity": 999})
    try:
        boundary.affirm(fake_context)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("fabricated context was affirmed")
    assert calls == []
    # The REAL rendered context still affirms correctly.
    boundary.affirm(real_context)
    assert calls == ["raw-token"]


def test_affirm_without_any_render_is_rejected():
    boundary = _boundary()
    boundary.preview(INTENT)
    try:
        boundary.affirm(object())  # never went through render_approval
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("affirmation without a prior render succeeded")


# --- B. Execution boundary lifecycle: single-proposal / single-use ---


def test_boundary_refuses_second_preview_without_reset():
    boundary = _boundary()
    boundary.preview(INTENT)
    second_intent = PreviewIntent(**{**INTENT.__dict__, "proposal_id": "p2"})
    try:
        boundary.preview(second_intent)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("second preview was accepted without reset")


def test_reset_clears_all_prior_proposal_state():
    boundary = _boundary()
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    boundary.reconcile_and_settle(current_quantity=0)

    boundary.reset()

    second_intent = PreviewIntent(**{**INTENT.__dict__, "proposal_id": "p2"})
    boundary.preview(second_intent)
    # No stale approval/broker-order/applied-fill state survives: reconciling
    # before a fresh render_approval/affirm on the new proposal must fail.
    try:
        boundary.reconcile_and_settle(current_quantity=0)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("stale approval state leaked across reset")


# --- C. Settlement direction comes from the approved proposal, not the caller ---


def test_approved_buy_cannot_be_settled_as_sell():
    """The old API let a caller pass action='SELL' independently of the
    approved BUY; the new API derives direction from the approved context,
    so there is no parameter through which a caller could flip it."""
    boundary = _boundary(reconcile=lambda ref: {"id": ref, "status": "FILLED", "filled": 1})
    boundary.preview(INTENT)  # INTENT.side == "BUY"
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    # Settlement must reflect a BUY (position increases), never a SELL.
    assert result.portfolio.owned_quantity == 1
    assert result.portfolio.sold_quantity == 0


def test_settled_state_transition_matches_the_approved_transition():
    boundary = _boundary(reconcile=lambda ref: {"id": ref, "status": "FILLED", "filled": 1})
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    assert result.portfolio.next_state == "HOLD"  # terminal full BUY -> HOLD


# --- D. Cumulative fill delta applies only the new increment ---


def test_partial_then_full_buy_applies_only_the_delta_each_time():
    orders = iter(
        [
            {"id": "broker-1", "status": "SUBMITTED", "filled": 0.4},
            {"id": "broker-1", "status": "FILLED", "filled": 1.0},
        ]
    )
    boundary = _boundary(reconcile=lambda ref: next(orders))
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)

    first = boundary.reconcile_and_settle(current_quantity=0)
    assert first.portfolio.owned_quantity == 0.4
    assert first.portfolio.next_state == "ENTER"  # nonterminal: still pending

    second = boundary.reconcile_and_settle(current_quantity=0.4)
    assert second.portfolio.owned_quantity == 1.0  # 0.4 + delta(0.6), not 0.4 + 1.0
    assert second.portfolio.next_state == "HOLD"  # terminal: transition completes


def test_repeated_identical_reconciliation_applies_zero_delta():
    boundary = _boundary(reconcile=lambda ref: {"id": ref, "status": "SUBMITTED", "filled": 0.4})
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)

    first = boundary.reconcile_and_settle(current_quantity=0)
    assert first.portfolio.owned_quantity == 0.4

    second = boundary.reconcile_and_settle(current_quantity=0.4)
    assert second.portfolio.owned_quantity == 0.4  # no duplicate application
    assert not second.portfolio.portfolio_mutated


# --- E. Partial nonterminal fills remain pending, not fully settled ---


def test_partial_buy_owns_shares_but_state_remains_pending_enter():
    boundary = _boundary(
        reconcile=lambda ref: {"id": "broker-1", "status": "SUBMITTED", "filled": 0.4}
    )
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    assert result.portfolio.owned_quantity == 0.4
    assert result.portfolio.next_state == "ENTER"
    assert result.portfolio.next_state != "HOLD"


# --- Restart recovery: reconstruct a boundary without re-previewing ---


def test_recover_previewed_can_finish_the_lifecycle_after_restart():
    calls = []
    boundary = Phase4ExecutionBoundary.recover_previewed(
        intent=INTENT,
        confirmation_token="recovered-raw-token",
        execution_ref="execution:p1",
        submit=lambda token: calls.append(token) or "broker-1",
        reconcile=lambda ref: {"id": ref, "status": "FILLED", "filled": 1},
        prove_paper=lambda: True,
    )
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0)
    assert calls == ["recovered-raw-token"]
    assert result.settlement.state == SettlementState.FILLED


def test_recover_previewed_resumes_applied_fill_after_restart():
    """A restart mid-partial-fill must not replay the already-applied delta."""
    boundary = Phase4ExecutionBoundary.recover_previewed(
        intent=INTENT,
        confirmation_token="recovered-raw-token",
        execution_ref="execution:p1",
        submit=lambda token: "broker-1",
        reconcile=lambda ref: {"id": ref, "status": "FILLED", "filled": 1.0},
        prove_paper=lambda: True,
        broker_order_ref="broker-1",
        already_applied_fill=0.4,
    )
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="score lineage"
    )
    boundary.affirm(context)
    result = boundary.reconcile_and_settle(current_quantity=0.4)
    assert result.portfolio.owned_quantity == 1.0  # 0.4 + delta(0.6), not 0.4+1.0

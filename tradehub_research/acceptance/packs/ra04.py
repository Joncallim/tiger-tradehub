"""RA-04: typed Phase-4 execution integration acceptance pack."""

from __future__ import annotations

from pathlib import Path

from tradehub.phase4_execution import ApprovalRequired, Phase4ExecutionBoundary
from tradehub_research.portfolio.execution import (
    PreviewIntent,
    SettlementState,
    apply_fill_to_portfolio,
    proposal_to_preview_intent,
    sanitize_settlement,
)

INTENT = PreviewIntent(
    proposal_id="proposal-1",
    symbol="AAPL",
    side="BUY",
    quantity=1,
    order_type="LIMIT",
    limit_price=150,
    currency="USD",
    score_snapshot_id="score-1",
    portfolio_snapshot_id="portfolio-1",
    policy_version="policy-1",
    reason="typed proposal execution; model prose excluded",
)


def _proposal() -> dict[str, object]:
    return {
        "proposal_id": "proposal-1",
        "security_id": "AAPL",
        "action": "BUY",
        "max_quantity_microunits": 1_000_000,
        "max_notional_microusd": 150_000_000,
        "score_snapshot_id": "score-1",
        "portfolio_snapshot_id": "portfolio-1",
        "policy_version": "policy-1",
    }


def _intent() -> PreviewIntent:
    return proposal_to_preview_intent(
        _proposal(),
        allowlist={"AAPL"},
        current_day_count=0,
        current_day_notional=0,
        max_day_count=3,
        max_day_notional=1000,
    )


def ra04_01_deterministic_translation(tmp: Path) -> None:
    assert _intent() == _intent()


def ra04_02_allowlist_fail_closed(tmp: Path) -> None:
    try:
        proposal_to_preview_intent(
            _proposal(),
            allowlist=set(),
            current_day_count=0,
            current_day_notional=0,
            max_day_count=3,
            max_day_notional=1000,
        )
    except ValueError:
        return
    raise AssertionError("empty allowlist was accepted")


def ra04_03_budget_revalidation(tmp: Path) -> None:
    for count, notional in ((3, 0), (0, 900)):
        try:
            proposal_to_preview_intent(
                _proposal(),
                allowlist={"AAPL"},
                current_day_count=count,
                current_day_notional=notional,
                max_day_count=3,
                max_day_notional=1000,
            )
        except ValueError:
            continue
        raise AssertionError("budget revalidation failed")


def ra04_04_rejected_preview_no_confirmation(tmp: Path) -> None:
    boundary = Phase4ExecutionBoundary(
        preview=lambda intent: {"accepted": False, "message": "rejected"},
        submit=lambda token: "never",
        reconcile=lambda ref: None,
        prove_paper=lambda: True,
    )
    try:
        boundary.preview(INTENT)
    except ApprovalRequired:
        return
    raise AssertionError("rejected preview remained actionable")


def ra04_05_exact_approval_and_paper_gate(tmp: Path) -> None:
    calls: list[str] = []
    boundary = Phase4ExecutionBoundary(
        preview=lambda intent: {"accepted": True, "confirmation_token": "opaque-token"},
        submit=lambda token: calls.append(token) or "broker-1",
        reconcile=lambda ref: {"id": ref, "status": "SUBMITTED", "filled": 0},
        prove_paper=lambda: True,
    )
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="pinned reason"
    )
    boundary.affirm(context, exact_order=context)
    assert calls == ["opaque-token"]


def ra04_06_nonpaper_blocks_submit(tmp: Path) -> None:
    calls: list[str] = []
    boundary = Phase4ExecutionBoundary(
        preview=lambda intent: {"accepted": True, "confirmation_token": "opaque-token"},
        submit=lambda token: calls.append(token) or "broker-1",
        reconcile=lambda ref: None,
        prove_paper=lambda: False,
    )
    boundary.preview(INTENT)
    context = boundary.render_approval(
        INTENT, current_state="WATCH", proposed_state="ENTER", rationale="pinned reason"
    )
    try:
        boundary.affirm(context, exact_order=context)
    except ApprovalRequired:
        assert calls == []
        return
    raise AssertionError("non-PAPER account submitted")


def ra04_07_full_fill(tmp: Path) -> None:
    s = sanitize_settlement(
        proposal_id="p", execution_ref="r", order={"status": "FILLED", "filled": 1}, requested_qty=1
    )
    assert s.state == SettlementState.FILLED


def ra04_08_partial_buy(tmp: Path) -> None:
    s = sanitize_settlement(
        proposal_id="p",
        execution_ref="r",
        order={"status": "SUBMITTED", "filled": 0.4},
        requested_qty=1,
    )
    p = apply_fill_to_portfolio(
        proposal_id="p",
        execution_ref="r",
        action="BUY",
        proposed_state="ENTER",
        current_quantity=0,
        settlement=s,
    )
    assert p.owned_quantity == 0.4


def ra04_09_partial_sell(tmp: Path) -> None:
    s = sanitize_settlement(
        proposal_id="p",
        execution_ref="r",
        order={"status": "SUBMITTED", "filled": 0.4},
        requested_qty=1,
    )
    p = apply_fill_to_portfolio(
        proposal_id="p",
        execution_ref="r",
        action="SELL",
        proposed_state="TRIM",
        current_quantity=1,
        settlement=s,
    )
    assert p.sold_quantity == 0.4 and p.owned_quantity == 0.6


def ra04_10_zero_fill_enter(tmp: Path) -> None:
    s = sanitize_settlement(
        proposal_id="p", execution_ref="r", order={"status": "OPEN", "filled": 0}, requested_qty=1
    )
    p = apply_fill_to_portfolio(
        proposal_id="p",
        execution_ref="r",
        action="BUY",
        proposed_state="ENTER",
        current_quantity=0,
        settlement=s,
    )
    assert not p.portfolio_mutated and p.next_state == "ENTER"


def ra04_11_cancel_expire_reject(tmp: Path) -> None:
    for status in ("CANCELLED", "EXPIRED", "REJECTED"):
        s = sanitize_settlement(
            proposal_id="p",
            execution_ref="r",
            order={"status": status, "filled": 0},
            requested_qty=1,
        )
        p = apply_fill_to_portfolio(
            proposal_id="p",
            execution_ref="r",
            action="BUY",
            proposed_state="ENTER",
            current_quantity=0,
            settlement=s,
        )
        assert not p.portfolio_mutated


def ra04_12_indeterminate_fail_closed(tmp: Path) -> None:
    s = sanitize_settlement(proposal_id="p", execution_ref="r", order=None, requested_qty=1)
    p = apply_fill_to_portfolio(
        proposal_id="p",
        execution_ref="r",
        action="BUY",
        proposed_state="ENTER",
        current_quantity=0,
        settlement=s,
    )
    assert s.state == SettlementState.INDETERMINATE and not p.portfolio_mutated


def ra04_13_sanitized_no_token(tmp: Path) -> None:
    s = sanitize_settlement(
        proposal_id="p",
        execution_ref="r",
        order={"id": "broker-1", "status": "FILLED", "filled": 1},
        requested_qty=1,
    )
    assert "token" not in repr(s).lower()


ASSERTIONS = [
    ("RA04-01", ra04_01_deterministic_translation),
    ("RA04-02", ra04_02_allowlist_fail_closed),
    ("RA04-03", ra04_03_budget_revalidation),
    ("RA04-04", ra04_04_rejected_preview_no_confirmation),
    ("RA04-05", ra04_05_exact_approval_and_paper_gate),
    ("RA04-06", ra04_06_nonpaper_blocks_submit),
    ("RA04-07", ra04_07_full_fill),
    ("RA04-08", ra04_08_partial_buy),
    ("RA04-09", ra04_09_partial_sell),
    ("RA04-10", ra04_10_zero_fill_enter),
    ("RA04-11", ra04_11_cancel_expire_reject),
    ("RA04-12", ra04_12_indeterminate_fail_closed),
    ("RA04-13", ra04_13_sanitized_no_token),
]

"""Autonomous PAPER acceptance (issue #51 O).

Proves the deterministic runner + policy + kill switch + budgets + PAPER
proof contract. No real broker is contacted: a FakeClient stands in for the
execution API; the runner's logic is exercised with injectable clock, proof,
and allowlist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradehub_research.autonomy import kill_switch, policy
from tradehub_research.autonomy.policy import PaperAutonomyPolicy
from tradehub_research.autonomy.runner import run_autonomy
from tradehub_research.config import ResearchSettings

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
PAPER_PROOF = {
    "environment": "LIVE",
    "account": "21155143479478282",
    "account_type": "PAPER",  # broker's own assertion (Tiger paper accounts live on the production API)
    "account_status": "Funded",
    "assets_ok": True,
    "proven_at": "2026-09-01T11:59:00Z",
}
ALLOWLIST = {"AAPL", "MSFT", "NVDA"}


class FakeClient:
    """Deterministic stand-in for the execution API (no broker contact)."""

    def __init__(self, proof: dict | None = None, allowlist=None) -> None:
        self.proof = proof or PAPER_PROOF
        self.allowlist = list(allowlist or ALLOWLIST)
        self.calls: list[tuple[str, dict | None]] = []
        self.preview_ok = True
        self.submit_response = {"submitted": True, "dry_run": True, "order_id": "o-1"}
        self.reconcile_response = {"status": "FILLED", "submitted": True, "order_id": "o-1"}

    def get(self, path: str) -> dict:
        self.calls.append((path, None))
        if path == "/account/proof":
            return self.proof
        if path == "/config/allowlist":
            return {"symbols": self.allowlist}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if path == "/orders/preview":
            if not self.preview_ok:
                return {"accepted": False, "policy_warnings": ["preview rejected"]}
            return {"accepted": True, "confirmation_token": "tok-" + str(len(self.calls))}
        if path == "/orders/submit":
            if isinstance(self.submit_response, Exception):
                raise self.submit_response
            return self.submit_response
        if path == "/orders/submit/reconcile":
            return self.reconcile_response
        raise AssertionError(f"unexpected POST {path}")


def _envelope(
    proposal_id: str = "prop-1",
    *,
    action: str = "BUY",
    symbol: str = "AAPL",
    data_as_of: str = "2026-08-31",
    fixture: bool = False,
    fixture_tag: str | None = None,
    created_at: str | None = None,
    quantity: int = 100_000_000,
    notional: int = 1_000_000_000,
    weight: int = 10_000,
    current_qty: int | None = None,
    sellable: int | None = None,
    mark: int = 15_000_000,
    security_id: str = "S1",
) -> dict:
    proposal = {
        "proposal_id": proposal_id,
        "security_id": security_id,
        "action": action,
        "max_quantity_microunits": quantity,
        "completion_quantity_microunits": quantity,
        "max_notional_microusd": notional,
        "target_weight_ppm": weight,
        "current_weight_ppm": 0 if action == "BUY" else weight,
        "current_quantity_microunits": current_qty,
        "sellable_quantity_microunits": sellable,
        "mark_price_microusd": mark,
        "score_snapshot_id": "score-1",
        "portfolio_snapshot_id": "pf-1",
        "policy_version": "v1",
        "sizing_policy_version": "v1",
        "quantity_increment_microunits": 1,
        "limit_only": True,
        "created_at": created_at or "2026-08-31T12:00:00Z",
    }
    envelope = {
        "proposal": proposal,
        "symbol": symbol,
        "data_as_of": data_as_of,
        "universe": "US_STOCKS",
        "exported_at": "2026-08-31T12:05:00Z",
    }
    if fixture:
        envelope["fixture"] = True
        envelope["fixture_tag"] = fixture_tag or "paper-acceptance-fixture-v1"
    return envelope


@pytest.fixture()
def ctx(tmp_path):
    """Isolated runner context: policy, kill switch, budgets, inbox."""
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    policy_file = policy_dir / "paper_policy.json"
    policy_file.write_text(json.dumps(policy.default_policy_payload()))
    kill_file = tmp_path / "kill_switch"
    inbox = tmp_path / "inbox"
    ledger = tmp_path / "ledger.jsonl"
    budget_db = tmp_path / "budget.sqlite"
    settings = ResearchSettings(api_token="test-token")
    return {
        "policy_path": policy_file,
        "kill_file": kill_file,
        "inbox": inbox,
        "ledger": ledger,
        "budget_db": budget_db,
        "settings": settings,
        "client": FakeClient(),
    }


def _write_inbox(ctx, envelope) -> Path:
    ctx["inbox"].mkdir(parents=True, exist_ok=True)
    path = ctx["inbox"] / f"{envelope['proposal']['proposal_id']}.json"
    path.write_text(json.dumps(envelope))
    return path


def _run(ctx, **kwargs):
    return run_autonomy(
        settings=ctx["settings"],
        policy_path=ctx["policy_path"],
        inbox=ctx["inbox"],
        ledger=ctx["ledger"],
        budget_db=ctx["budget_db"],
        api_client=ctx["client"],
        now=NOW,
        **kwargs,
    )


def test_policy_is_versioned_and_paper_provisional(ctx):
    loaded = policy.load_policy(ctx["policy_path"])
    assert isinstance(loaded, PaperAutonomyPolicy)
    assert loaded.policy_version == "paper-autonomy-v1"
    assert loaded.account_mode == "PAPER"
    assert loaded.label == "PAPER_PROVISIONAL"
    assert loaded.is_paper


def test_autonomy_disabled_yields_zero_writes(ctx):
    raw = json.loads(ctx["policy_path"].read_text())
    raw["enabled"] = False
    ctx["policy_path"].write_text(json.dumps(raw))
    _write_inbox(ctx, _envelope())
    summary = _run(ctx)
    assert summary["status"] == "DISABLED"
    assert summary["orders"] == 0
    assert ctx["client"].calls == []  # no API call at all


def test_kill_switch_blocks_before_run(ctx):
    kill_switch.set_blocked(ctx["kill_file"])
    _write_inbox(ctx, _envelope())
    summary = _run(ctx, kill_switch_path=ctx["kill_file"])
    assert summary["status"] == "BLOCKED"
    assert summary["orders"] == 0


def test_kill_switch_survives_restart(ctx):
    kill_switch.set_blocked(ctx["kill_file"])
    # "restart": a fresh is_blocked() from the file (no in-memory state).
    assert kill_switch.is_blocked(ctx["kill_file"]) is True
    kill_switch.clear_blocked(ctx["kill_file"])
    assert kill_switch.is_blocked(ctx["kill_file"]) is False


def test_non_paper_environment_yields_zero_writes(ctx):
    ctx["client"].proof = {**PAPER_PROOF, "account_type": "LIVE_ACCOUNT"}
    _write_inbox(ctx, _envelope())
    summary = _run(ctx)
    assert summary["status"] == "REFUSED"
    assert summary["orders"] == 0
    assert "PAPER proof failed" in summary["reason"]


def test_stale_data_yields_zero_writes(ctx):
    _write_inbox(ctx, _envelope(data_as_of="2026-07-01"))  # 62 days old
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any("stale" in r["reason"].lower() for r in summary["refusals"])


def test_stale_proposal_yields_zero_writes(ctx):
    _write_inbox(ctx, _envelope(created_at="2026-01-01T00:00:00Z"))
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any("stale" in r["reason"].lower() for r in summary["refusals"])


def test_allowlist_failure_yields_zero_writes(ctx):
    _write_inbox(ctx, _envelope(symbol="ZZZZ"))
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any("allowlist" in r["reason"] for r in summary["refusals"])


def test_untyped_envelope_yields_zero_writes(ctx):
    ctx["inbox"].mkdir(parents=True, exist_ok=True)
    (ctx["inbox"] / "bad.json").write_text(json.dumps({"symbol": "AAPL"}))  # no typed proposal
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any("typed proposal" in r["reason"] for r in summary["refusals"])


def test_daily_order_count_budget(ctx):
    payload = policy.default_policy_payload()
    payload["max_order_count_per_day"] = 1
    ctx["policy_path"].write_text(json.dumps(payload))
    _write_inbox(ctx, _envelope("prop-a"))
    _write_inbox(ctx, _envelope("prop-b"))
    summary = _run(ctx)
    assert summary["orders"] == 1
    assert any("order-count budget" in r["reason"] for r in summary["refusals"])


def test_daily_notional_budget(ctx):
    payload = policy.default_policy_payload()
    payload["max_notional_per_day_microusd"] = 1_500_000_000  # $1,500
    ctx["policy_path"].write_text(json.dumps(payload))
    _write_inbox(ctx, _envelope("prop-a", notional=1_000_000_000))  # $1,000
    _write_inbox(ctx, _envelope("prop-b", notional=1_000_000_000))  # $1,000 -> over
    summary = _run(ctx)
    assert summary["orders"] == 1
    assert any("notional budget" in r["reason"] for r in summary["refusals"])


def test_long_only_holding_bounds(ctx):
    _write_inbox(
        ctx,
        _envelope(
            "prop-sell",
            action="SELL",
            quantity=500_000_000,
            current_qty=100_000_000,
            sellable=100_000_000,
        ),
    )
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any(
        "sellable" in r["reason"] or "current holdings" in r["reason"] for r in summary["refusals"]
    )


def test_exposure_bound(ctx):
    payload = policy.default_policy_payload()
    payload["max_per_position_exposure_ppm"] = 5_000  # 0.5%
    ctx["policy_path"].write_text(json.dumps(payload))
    _write_inbox(ctx, _envelope("prop-w", weight=100_000))  # 10%
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any("exposure" in r["reason"] for r in summary["refusals"])


def test_one_proposal_max_one_order_and_idempotent(ctx):
    _write_inbox(ctx, _envelope("prop-1"))
    first = _run(ctx)
    assert first["orders"] == 1
    submits = [c for c in ctx["client"].calls if c[0] == "/orders/submit"]
    assert len(submits) == 1
    # Duplicate invocation (restart / double timer): the proposal was charged
    # and archived; a re-run must NOT place a second order.
    second = _run(ctx)
    assert second["orders"] == 0
    submits_after = [c for c in ctx["client"].calls if c[0] == "/orders/submit"]
    assert len(submits_after) == 1


def test_kill_switch_between_preview_and_submit(ctx):
    """The runner re-checks the kill switch immediately before the write."""
    original = kill_switch.is_blocked

    def flip_after_preview(path=None):
        # After the first /orders/preview call, engage the switch.
        if sum(1 for c in ctx["client"].calls if c[0] == "/orders/preview") >= 1:
            return True
        return original(path)

    kill_switch.is_blocked = flip_after_preview  # type: ignore[assignment]
    try:
        _write_inbox(ctx, _envelope("prop-ks"))
        summary = _run(ctx, kill_switch_path=ctx["kill_file"])
        assert summary["orders"] == 0
        assert any("kill switch" in r["reason"] for r in summary["refusals"])
    finally:
        kill_switch.is_blocked = original  # type: ignore[assignment]


def test_indeterminate_submit_is_recorded(ctx):
    ctx["client"].submit_response = RuntimeError("submit_indeterminate (5xx)")
    _write_inbox(ctx, _envelope("prop-ind"))
    summary = _run(ctx)
    assert summary["orders"] == 1  # the attempt was recorded, not lost
    assert summary["executions"][0]["decision"] == "INDETERMINATE"
    assert "submit_indeterminate" in summary["executions"][0]["submit_error"]


def test_no_action_cycle_is_valid(ctx):
    summary = _run(ctx)  # empty inbox
    assert summary["status"] == "OK"
    assert summary["orders"] == 0
    assert summary["proposals_seen"] == 0


def test_fixture_proposal_requires_marked_tag(ctx):
    _write_inbox(ctx, _envelope("prop-fx", fixture=True, fixture_tag="wrong"))
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert any("acceptance-fixture" in r["reason"] for r in summary["refusals"])


def test_runner_has_no_llm_and_no_raw_evidence(tmp_path):
    src = (
        Path(__file__).parent.parent / "tradehub_research" / "autonomy" / "runner.py"
    ).read_text()
    for token in ("openai", "anthropic", "claude", "delegate_task", "httpx.post("):
        assert token not in src.lower(), f"runner must not reference {token!r}"
    # The runner consumes typed proposals only -- no evidence/file fields.
    assert "raw_features" not in src
    assert "evidence_pack" not in src


def test_execution_api_enforces_kill_switch_on_autonomous_submit(tmp_path):
    """The app's submit path blocks autonomous submits while the kill switch
    is engaged (static contract: the guard exists and reads the switch)."""
    app_src = (Path(__file__).parent.parent / "tradehub" / "app.py").read_text()
    assert "autonomous writes BLOCKED by kill switch" in app_src
    assert "is_blocked()" in app_src

"""Systemd authority boundary, path-trigger, and PAPER autonomy transition gates.

Covers the four exact-head findings:
  1. research/finalizer may write ONLY the narrow authority directory
  2. the autonomy path unit triggers on inbox CHANGE, never on file EXISTENCE
  3. policy.allowed_state_transitions is ENFORCED before budget charge/preview
  4. the empty-inbox path leaves durable evidence that it contacted no broker
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_autonomy_paper import _envelope, _run, _write_inbox, ctx  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"

RESEARCH_SERVICES = (
    "tradehub-research-cycle.service",
    "tradehub-committee-finalizer.service",
)
AUTHORITY_DIR = "/var/lib/tradehub/autonomy/authority"


# --------------------------------------------------------------------------
# 1. systemd sandbox: narrow authority write path, no broader widening
# --------------------------------------------------------------------------
@pytest.mark.parametrize("unit", RESEARCH_SERVICES)
def test_research_services_may_write_authority_dir_only(unit):
    text = (SYSTEMD / unit).read_text(encoding="utf-8")
    assert AUTHORITY_DIR in text, f"{unit} must be able to write the authority dir"
    assert "ProtectSystem=strict" in text
    # Must not widen the sandbox to the whole autonomy tree or to /var/lib/tradehub.
    rw = next(line for line in text.splitlines() if line.startswith("ReadWritePaths="))
    assert " /var/lib/tradehub " not in rw + " "
    assert not rw.rstrip().endswith("/var/lib/tradehub")
    assert not rw.rstrip().endswith("/var/lib/tradehub/autonomy")
    # The kill switch must stay non-writable.
    assert "InaccessiblePaths=/var/lib/tradehub/autonomy/kill_switch" in text


@pytest.mark.parametrize("unit", RESEARCH_SERVICES)
def test_research_services_cannot_read_execution_or_autonomy_env(unit):
    text = (SYSTEMD / unit).read_text(encoding="utf-8")
    assert "/etc/tradehub/execution.env" in text
    assert "/etc/tradehub/autonomy.env" in text
    assert "InaccessiblePaths=" in text


# --------------------------------------------------------------------------
# 2. path unit: change trigger, not existence trigger
# --------------------------------------------------------------------------
def test_autonomy_path_unit_triggers_on_change_not_existence():
    text = (SYSTEMD / "tradehub-paper-autonomy.path").read_text(encoding="utf-8")
    assert "PathChanged=/var/lib/tradehub/autonomy/proposals" in text
    assert "PathExistsGlob" not in text
    # The loop must be structurally absent, not merely rate-limited.
    assert "TriggerLimit" not in text


def test_autonomy_timer_is_a_bounded_recovery_poll():
    text = (SYSTEMD / "tradehub-paper-autonomy.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=30min" in text


# --------------------------------------------------------------------------
# 3. allowed_state_transitions is enforced before budget charge / preview
# --------------------------------------------------------------------------
ALLOWED = [
    ("BUY", "WATCH", "ENTER"),
    ("BUY", "HOLD", "ADD"),
    ("SELL", "HOLD", "TRIM"),
    ("SELL", "HOLD", "EXIT"),
    ("SELL", "TRIM", "EXIT"),
]
REFUSED = [
    ("BUY", "DISCOVER", "ENTER"),
    ("SELL", "WATCH", "EXIT"),
]


def _sell_envelope(**kwargs):
    """A SELL needs holdings to survive the exposure check."""
    return _envelope(action="SELL", current_qty=100_000_000, sellable=100_000_000, **kwargs)


@pytest.mark.parametrize(("action", "current", "proposed"), ALLOWED)
def test_allowed_transition_executes(ctx, action, current, proposed):  # noqa: F811
    env = (
        _envelope(action=action, current_state=current, proposed_state=proposed)
        if action == "BUY"
        else _sell_envelope(current_state=current, proposed_state=proposed)
    )
    _write_inbox(ctx, env)
    summary = _run(ctx)
    assert summary["orders"] == 1, summary["refusals"]
    assert summary["executions"][0]["decision"] == "EXECUTED"


@pytest.mark.parametrize(("action", "current", "proposed"), REFUSED)
def test_disallowed_transition_refused(ctx, action, current, proposed):  # noqa: F811
    env = (
        _envelope(action=action, current_state=current, proposed_state=proposed)
        if action == "BUY"
        else _sell_envelope(current_state=current, proposed_state=proposed)
    )
    _write_inbox(ctx, env)
    summary = _run(ctx)
    assert summary["orders"] == 0
    assert summary["executions"] == []
    assert any(
        "state transition" in str(entry.get("reason", "")) for entry in summary["refusals"]
    ), summary["refusals"]


def test_refused_transition_does_not_charge_budget_or_preview(ctx):  # noqa: F811
    """The transition gate must run BEFORE the budget charge and any preview."""
    _write_inbox(ctx, _envelope(current_state="DISCOVER", proposed_state="ENTER"))
    refused = _run(ctx)
    assert refused["orders"] == 0
    assert [path for path, _ in ctx["client"].calls if path == "/orders/preview"] == []

    # A subsequent VALID transition must still execute: if the refused run had
    # charged the daily budget, the counter would already be at 1.
    _write_inbox(ctx, _envelope(proposal_id="prop-2"))
    allowed = _run(ctx)
    assert allowed["orders"] == 1, allowed["refusals"]


def test_policy_allowed_transitions_is_unmodified():
    """Enforcement must not have changed the owner-approved list."""
    from tradehub.autonomy.policy import PAPER_PROVISIONAL_DEFAULTS

    assert list(PAPER_PROVISIONAL_DEFAULTS["allowed_state_transitions"]) == [
        "WATCH->ENTER",
        "HOLD->ADD",
        "HOLD->TRIM",
        "HOLD->EXIT",
        "TRIM->EXIT",
    ]


# --------------------------------------------------------------------------
# 4. empty inbox: durable evidence of no broker contact
# --------------------------------------------------------------------------
def test_empty_inbox_records_durable_receipt(ctx):  # noqa: F811
    summary = _run(ctx)
    assert summary["status"] == "IDLE_EMPTY_INBOX"
    assert ctx["client"].calls == []
    lines = [ln for ln in ctx["ledger"].read_text().splitlines() if ln.strip()]
    receipt = json.loads(lines[-1])
    assert receipt["kind"] == "runner_run_receipt_v1"
    assert receipt["status"] == "IDLE_EMPTY_INBOX"
    assert receipt["broker_contacted"] is False
    assert receipt["orders"] == 0
    assert receipt["proposals_seen"] == 0


# --------------------------------------------------------------------------
# 5. operator_status truthfulness (source guard on the named anti-pattern)
# --------------------------------------------------------------------------
def test_operator_status_never_labels_global_authority_count_as_latest_exports():
    src = (ROOT / "tradehub_research" / "ops" / "operator_status.py").read_text(encoding="utf-8")
    # Global/lifetime totals must be named as such...
    assert "authority_records_total" in src
    assert "portfolio_runs_total" in src
    # ...and never assigned to the latest decision's eligible_exports.
    assert 'chain["eligible_exports"]' not in src
    assert 'chain["authority_records"]' not in src
    # Per-decision values are derived from durable state, not from the cycle log.
    assert "latest_decision" in src
    assert "_published_authority_ids" in src

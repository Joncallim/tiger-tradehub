"""Report-truthfulness regressions for the autonomous runner ledger.

A run receipt is the durable record of ONE INVOCATION, not an action. Counting
receipts as refusals made an enabled-but-idle runner (the 30-minute recovery
timer writing IDLE_EMPTY_INBOX receipts) render as "N refused/blocked" in the
daily report -- a false production claim introduced by the durable idle
receipts themselves. CI never saw it because no live ledger exists there.
"""

from __future__ import annotations

import json
from datetime import date

from tradehub_research.ops.report_cli import _ledger_actions


def _write(tmp_path, entries):
    path = tmp_path / "paper_run_ledger.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def test_idle_run_receipts_are_not_actions(tmp_path):
    """The exact regression: 48 idle receipts must not become 48 'refusals'."""
    today = date.today().isoformat()
    ledger = _write(
        tmp_path,
        [
            {
                "kind": "runner_run_receipt_v1",
                "status": "IDLE_EMPTY_INBOX",
                "orders": 0,
                "proposals_seen": 0,
                "refusal_count": 0,
                "at": f"{today}T04:20:01Z",
            }
        ]
        * 48,
    )
    assert _ledger_actions(ledger, today) == (0, 0, 0)


def test_receipt_refusal_count_is_the_durable_refusal_total(tmp_path):
    today = date.today().isoformat()
    ledger = _write(
        tmp_path,
        [
            {
                "kind": "runner_run_receipt_v1",
                "status": "OK",
                "orders": 0,
                "proposals_seen": 2,
                "refusal_count": 2,
                "at": f"{today}T04:22:03Z",
            }
        ],
    )
    assert _ledger_actions(ledger, today) == (0, 2, 0)


def test_executed_and_indeterminate_entries_are_counted_separately(tmp_path):
    today = date.today().isoformat()
    ledger = _write(
        tmp_path,
        [
            {"proposal_id": "p1", "decision": "EXECUTED", "at": f"{today}T01:00:00Z"},
            {"proposal_id": "p2", "decision": "INDETERMINATE", "at": f"{today}T02:00:00Z"},
            {"proposal_id": "p3", "at": f"{today}T02:30:00Z"},
        ],
    )
    # An indeterminate submit is UNKNOWN -- never reported as a refusal.
    assert _ledger_actions(ledger, today) == (1, 0, 2)


def test_other_days_and_malformed_lines_are_ignored(tmp_path):
    today = date.today().isoformat()
    path = tmp_path / "paper_run_ledger.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"proposal_id": "old", "decision": "EXECUTED", "at": "2026-01-01T00:00:00Z"})
        + "\n"
        + json.dumps(["not", "an", "object"])
        + "\n"
        + json.dumps({"proposal_id": "p1", "decision": "EXECUTED", "at": f"{today}T09:00:00Z"})
        + "\n"
    )
    assert _ledger_actions(path, today) == (1, 0, 0)


def test_absent_ledger_is_a_known_zero(tmp_path):
    assert _ledger_actions(tmp_path / "nope.jsonl", date.today().isoformat()) == (0, 0, 0)

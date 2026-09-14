"""Report-truthfulness regressions for the autonomous runner ledger.

A run receipt is the durable record of ONE INVOCATION, not an action. Counting
receipts as refusals made an enabled-but-idle runner (the 30-minute recovery
timer writing IDLE_EMPTY_INBOX receipts) render as "N refused/blocked" in the
daily report -- a false production claim introduced by the durable idle
receipts themselves. CI never saw it because no live ledger exists there.

The second half pins the review finding: an EXISTING but UNREADABLE ledger must
be reported as unavailable (never as a zero-action day) and must never raise out
of daily/weekly report generation.
"""

from __future__ import annotations

import json
from datetime import date

from tradehub_research.ops.report_cli import _history, _ledger_actions

TRUNCATED = b'{"kind": "runner_run_receipt_v1", "at": "2026-09-14T00:00:00Z"}\n\xff\xfe mid-byte\n'


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
    assert _ledger_actions(ledger, today) == (0, 0, 0, None)


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
    assert _ledger_actions(ledger, today) == (0, 2, 0, None)


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
    assert _ledger_actions(ledger, today) == (1, 0, 2, None)


def test_a_truncated_partial_line_is_skipped_not_fatal(tmp_path):
    """The realistic corruption: an ASCII write cut mid-line."""
    today = date.today().isoformat()
    path = tmp_path / "paper_run_ledger.jsonl"
    complete = f'{{"proposal_id": "p1", "decision": "EXECUTED", "at": "{today}T09:00:00Z"}}\n'
    path.write_text(complete + '{"proposal_id": "p2", "dec')
    assert _ledger_actions(path, today) == (1, 0, 0, None)


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
    assert _ledger_actions(path, today) == (1, 0, 0, None)


def test_absent_ledger_is_a_known_zero(tmp_path):
    assert _ledger_actions(tmp_path / "nope.jsonl", date.today().isoformat()) == (0, 0, 0, None)


# --------------------------------------------------------------------------
# review finding: an EXISTING but UNREADABLE ledger
# --------------------------------------------------------------------------
def test_undecodable_ledger_is_unknown_not_zero_and_does_not_raise(tmp_path):
    """A write truncated mid-byte must not crash the surface nor fake a zero day."""
    path = tmp_path / "paper_run_ledger.jsonl"
    path.write_bytes(TRUNCATED)
    acts = _ledger_actions(path, date.today().isoformat())
    assert acts.executions is None
    assert acts.refusals is None
    assert acts.unknown is None
    assert acts.error == "UnicodeDecodeError"


def test_unreadable_ledger_is_unknown_not_zero(tmp_path):
    class _Unreadable:
        def exists(self):
            return True

        def read_text(self, *args, **kwargs):
            raise PermissionError("EACCES")

    acts = _ledger_actions(_Unreadable(), date.today().isoformat())
    assert (acts.executions, acts.refusals, acts.unknown) == (None, None, None)
    assert acts.error == "PermissionError"


def test_undecodable_history_is_empty_and_does_not_raise(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"date":"2026-09-01","asset_value":1}\n\xff\xfe\n')
    assert _history(path) == []


def test_unreadable_history_is_empty_and_does_not_raise(tmp_path):
    class _Unreadable:
        def exists(self):
            return True

        def read_text(self, *args, **kwargs):
            raise OSError("EIO")

    assert _history(_Unreadable()) == []


def test_daily_report_survives_an_unreadable_ledger(tmp_path, monkeypatch):
    """End-to-end: the report still renders and says the ledger is unavailable."""
    from types import SimpleNamespace

    from tradehub_research.ops import report_cli

    class _Unreadable:
        def exists(self):
            return True

        def read_text(self, *args, **kwargs):
            raise PermissionError("EACCES")

    monkeypatch.setattr(report_cli, "LEDGER", _Unreadable())
    monkeypatch.setattr(
        report_cli,
        "forward_health",
        lambda **kw: {"production_predictions": 0, "predictions_due": 0, "matured": {}},
    )
    monkeypatch.setattr(
        report_cli,
        "refresh_health",
        lambda **kw: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    paths = SimpleNamespace(
        research_db=tmp_path / "r.db",
        experiment_db=tmp_path / "e.db",
        research_dir=tmp_path,
    )
    report = report_cli.build_daily_report(
        settings=SimpleNamespace(busy_timeout_ms=5000),
        experiment_db=None,
        paths=paths,
        analytics={},
    )
    assert "action ledger unavailable (PermissionError)" in report
    assert "No action" not in report


def test_weekly_report_survives_an_unreadable_ledger(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tradehub_research.ops import report_cli

    class _Unreadable:
        def exists(self):
            return True

        def read_text(self, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(report_cli, "LEDGER", _Unreadable())
    monkeypatch.setattr(
        report_cli,
        "forward_health",
        lambda **kw: {"production_predictions": 0, "predictions_due": 0, "matured": {}},
    )
    monkeypatch.setattr(
        report_cli,
        "refresh_health",
        lambda **kw: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    paths = SimpleNamespace(
        research_db=tmp_path / "r.db",
        experiment_db=tmp_path / "e.db",
        research_dir=tmp_path,
    )
    report = report_cli.build_weekly_report(
        settings=SimpleNamespace(busy_timeout_ms=5000),
        experiment_db=None,
        paths=paths,
        analytics={},
        history=[],
    )
    # UNKNOWN actions are rendered as unavailable, never as a zero count.
    assert "Trades: unavailable" in report
    assert "Blocked/refused: unavailable" in report
    assert "action ledger unavailable (UnicodeDecodeError)" in report

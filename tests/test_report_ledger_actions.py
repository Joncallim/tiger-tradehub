"""Report-truthfulness regressions for the autonomous runner ledger.

A run receipt is the durable record of ONE INVOCATION, not an action. Counting
receipts as refusals made an enabled-but-idle runner (the 30-minute recovery
timer writing IDLE_EMPTY_INBOX receipts) render as "N refused/blocked" in the
daily report -- a false production claim introduced by the durable idle
receipts themselves. CI never saw it because no live ledger exists there.

The second half pins two review findings:

* an EXISTING but UNREADABLE ledger must be reported as unavailable (never as a
  zero-action day) and must never raise out of daily/weekly report generation;
* existence itself must be probed with ``stat()``, because ``Path.exists()``
  swallows EACCES and returns False -- which is how a real read failure rendered
  as "No action" for the deployed report identity.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradehub_research.ops.report_cli import LedgerActions, _history, _ledger_actions

TRUNCATED = b'{"kind": "runner_run_receipt_v1", "at": "2026-09-14T00:00:00Z"}\n\xff\xfe mid-byte\n'


def _write(tmp_path, entries):
    path = tmp_path / "paper_run_ledger.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def _read_report(tmp_path, monkeypatch, builder, **kwargs):
    from tradehub_research.ops import report_cli

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
    return builder(
        settings=SimpleNamespace(busy_timeout_ms=5000),
        experiment_db=None,
        paths=paths,
        **kwargs,
    )


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
    """The realistic ASCII corruption: a write cut mid-line."""
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
# an EXISTING but UNREADABLE ledger is UNKNOWN, never a zero-action day
# --------------------------------------------------------------------------
class _LyingExists:
    """A path whose exists() says False while a real stat() raises EACCES.

    This is exactly the production shape: ``Path.exists()`` swallows EACCES and
    returns False, so the guard must not be built on it.
    """

    def __init__(self, exc):
        self._exc = exc

    def exists(self):
        return False

    def stat(self):
        raise self._exc

    def read_text(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("read_text must not be reached after a failed stat")


def test_eacces_that_exists_swallows_is_still_reported_as_unknown(tmp_path):
    acts = _ledger_actions(_LyingExists(PermissionError("EACCES")), date.today().isoformat())
    assert (acts.executions, acts.refusals, acts.unknown) == (None, None, None)
    assert acts.error == "PermissionError", "a swallowed EACCES must not read as 'No action'"


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
        def stat(self):
            return SimpleNamespace()

        def read_text(self, *args, **kwargs):
            raise PermissionError("EACCES")

    acts = _ledger_actions(_Unreadable(), date.today().isoformat())
    assert (acts.executions, acts.refusals, acts.unknown) == (None, None, None)
    assert acts.error == "PermissionError"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file mode bits")
def test_ledger_unreadable_by_real_permissions_is_unknown(tmp_path):
    """The real failure mode, not a mock: a ledger the reader cannot open."""
    path = tmp_path / "paper_run_ledger.jsonl"
    path.write_text("{}\n")
    path.chmod(0o000)
    try:
        assert path.exists() is True  # the file IS there
        acts = _ledger_actions(path, date.today().isoformat())
    finally:
        path.chmod(0o600)
    assert acts.error == "PermissionError", acts
    assert acts.executions is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode bits")
def test_ledger_behind_an_unsearchable_directory_is_unknown(tmp_path):
    """The deployed shape: the ledger sits in a directory the reader cannot enter."""
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "paper_run_ledger.jsonl"
    path.write_text("{}\n")
    locked.chmod(0o000)
    try:
        assert path.exists() is False, "exists() swallows the EACCES -- the trap"
        acts = _ledger_actions(path, date.today().isoformat())
    finally:
        locked.chmod(0o700)
    assert acts.error == "PermissionError", acts
    assert (acts.executions, acts.refusals, acts.unknown) == (None, None, None)


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------
def test_history_absent_is_an_empty_history(tmp_path):
    loaded = _history(tmp_path / "nope.jsonl")
    assert loaded.rows == []
    assert loaded.error is None


def test_undecodable_history_reports_the_error(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_bytes(b'{"date":"2026-09-01","asset_value":1}\n\xff\xfe\n')
    loaded = _history(path)
    assert loaded.rows == []
    assert loaded.error == "UnicodeDecodeError"


def test_unreadable_history_reports_the_error(tmp_path):
    loaded = _history(_LyingExists(OSError("EIO")))
    assert loaded.rows == []
    assert loaded.error == "OSError"


# --------------------------------------------------------------------------
# end-to-end: report generation survives and stays honest
# --------------------------------------------------------------------------
def test_daily_report_survives_an_unreadable_ledger(tmp_path, monkeypatch):
    from tradehub_research.ops import report_cli

    monkeypatch.setattr(report_cli, "LEDGER", _LyingExists(PermissionError("EACCES")))
    report = _read_report(tmp_path, monkeypatch, report_cli.build_daily_report, analytics={})
    assert "action ledger unavailable (PermissionError)" in report
    assert "No action" not in report


def test_weekly_report_survives_an_unreadable_ledger(tmp_path, monkeypatch):
    from tradehub_research.ops import report_cli

    monkeypatch.setattr(report_cli, "LEDGER", _LyingExists(PermissionError("EACCES")))
    monkeypatch.setattr(report_cli, "HISTORY", _LyingExists(PermissionError("EACCES")))
    report = _read_report(
        tmp_path, monkeypatch, report_cli.build_weekly_report, analytics={}, history=None
    )
    # UNKNOWN actions/history are rendered as unavailable, never as zero counts.
    assert "Trades: unavailable" in report
    assert "Blocked/refused: unavailable" in report
    assert "action ledger unavailable (PermissionError)" in report
    assert "broker history unavailable (PermissionError)" in report


def test_healthy_ledger_still_counts_normally(tmp_path):
    today = date.today().isoformat()
    ledger = _write(
        tmp_path,
        [{"proposal_id": "p1", "decision": "EXECUTED", "at": f"{today}T01:00:00Z"}],
    )
    acts = _ledger_actions(ledger, today)
    assert isinstance(acts, LedgerActions)
    assert acts == (1, 0, 0, None)
    assert Path(ledger).is_file()

"""The reports must not call legitimately-excluded names "stale data".

Live defect (found 2026-09-25): the daily/weekly reports counted staleness with
``refresh_health``'s crude 7-day cutoff -- "42 stale data names". The freshness
system's OWN authority (``ops/data_freshness.audit_universe``, which the health
watch diagnoses and remediates against) reported for the same universe: 443
securities = 40 legitimately unfetchable exceptions (delisted / invalid symbol /
no-trade, already excluded from eligibility) + 2 genuinely unresolved names
(LCGMF, TRLEF -- FETCHED_NOT_STORED since 2026-09-14, attempted, never stored).

So the headline number the owner read every night overstated the real backlog
~20x, and it never named the two names that actually need attention. Two
subsystems describing one thing must not disagree, and the report must not
promote a design-level exclusion into an incident.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from tradehub_research.ops import health, report_cli

LIVE_AUDIT = {
    "universe": 443,
    "excluded_exceptions": 40,
    "lagging_within_window": 401,
    "unresolved": ["LCGMF", "TRLEF"],
    "unresolved_count": 2,
}


def _fwd(production_predictions: int = 116_952, due: int = 0) -> dict:
    return {"production_predictions": production_predictions, "predictions_due": due, "matured": {}}


def test_health_line_names_the_unresolved_names_and_not_the_exclusions():
    line = report_cli._data_health_line(_fwd(), LIVE_AUDIT, None)
    assert "LCGMF" in line and "TRLEF" in line
    assert "2 unresolved" in line
    assert "40" in line and "legitimate" in line.lower()


def test_health_line_never_claims_a_stale_count_from_a_crude_cutoff():
    line = report_cli._data_health_line(_fwd(), LIVE_AUDIT, None)
    assert "stale" not in line.lower(), line


def test_health_line_says_so_when_there_are_no_unresolved_gaps():
    fresh = {**LIVE_AUDIT, "unresolved": [], "unresolved_count": 0}
    line = report_cli._data_health_line(_fwd(), fresh, None)
    assert "no unresolved data gaps" in line
    assert "LCGMF" not in line


def test_health_line_reports_the_audit_error_instead_of_a_plausible_number():
    line = report_cli._data_health_line(_fwd(), None, "OperationalError")
    assert "OperationalError" in line
    assert "42" not in line


def test_health_line_still_surfaces_forward_ledger_flags():
    due = report_cli._data_health_line(_fwd(production_predictions=0, due=7), LIVE_AUDIT, None)
    assert "no production predictions" in due
    assert "7 outcomes mature" in due


def _settings(tmp_path):
    return SimpleNamespace(busy_timeout_ms=5000, adapter_cache_dir=tmp_path / "raw")


def _paths(tmp_path):
    return SimpleNamespace(
        research_db=tmp_path / "research.db",
        experiment_db=tmp_path / "experiment.db",
        research_dir=tmp_path,
        raw_cache=tmp_path / "raw",
    )


def _stub_report_deps(monkeypatch, tmp_path, accounting=LIVE_AUDIT, error=None):
    monkeypatch.setattr(report_cli, "forward_health", lambda **kw: _fwd())
    monkeypatch.setattr(
        report_cli,
        "refresh_health",
        lambda **kw: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    monkeypatch.setattr(report_cli, "LEDGER", tmp_path / "absent.jsonl")
    if error is None:
        monkeypatch.setattr(report_cli, "freshness_accounting", lambda **kw: accounting)
    else:

        def _boom(**kwargs):
            raise error

        monkeypatch.setattr(report_cli, "freshness_accounting", _boom)


def test_daily_report_uses_the_audit_authority(tmp_path, monkeypatch):
    _stub_report_deps(monkeypatch, tmp_path)
    report = report_cli.build_daily_report(
        settings=_settings(tmp_path),
        experiment_db=None,
        paths=_paths(tmp_path),
        analytics={"asset_value": 1_000_000.0, "cash_balance": 1_000_000.0},
    )
    assert "LCGMF" in report and "TRLEF" in report
    assert "42 stale" not in report
    assert "stale data names" not in report


def test_daily_report_survives_an_audit_failure(tmp_path, monkeypatch):
    _stub_report_deps(
        monkeypatch, tmp_path, error=sqlite3.OperationalError("unable to open database file")
    )
    report = report_cli.build_daily_report(
        settings=_settings(tmp_path),
        experiment_db=None,
        paths=_paths(tmp_path),
        analytics={},
    )
    assert "OperationalError" in report, report


def test_freshness_accounting_matches_the_audit_it_reads(tmp_path, monkeypatch):
    """The report's accounting is the audit's, not a re-derivation of it."""
    from tradehub_research.ops import data_freshness as df

    class _Audit:
        universe_total = 443
        excluded_exceptions = 40
        lagging_within_window = ["A", "B"]
        stale_count = 2
        stale = [
            SimpleNamespace(ticker="LCGMF"),
            SimpleNamespace(ticker="TRLEF"),
        ]

    monkeypatch.setattr(df, "audit_universe", lambda **kw: _Audit())
    got = health.freshness_accounting(
        settings=SimpleNamespace(busy_timeout_ms=5000),
        paths=SimpleNamespace(research_dir=tmp_path),
        experiment_db=None,
    )
    assert got == {
        "universe": 443,
        "excluded_exceptions": 40,
        "lagging_within_window": 2,
        "unresolved": ["LCGMF", "TRLEF"],
        "unresolved_count": 2,
    }

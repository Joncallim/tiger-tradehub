"""Daily report: "New matured outcomes" must mean NEW TODAY.

Pre-fix, ``build_daily_report`` set ``new_matured = sum(matured_by_horizon.values())``
-- the LIFETIME count of appended outcomes -- so the daily line grew monotonically
and never reset. These tests pin the corrected semantics:

* "Predictions" = total production forward predictions;
* "New matured outcomes" = production ``forward_outcome`` rows appended on the
  reporting day, taken from the durable ``appended_at`` timestamp (never inferred
  from a prediction's due date);
* historical outcomes never count as new, and today's stop being new tomorrow;
* only ``provenance='production'`` outcomes contribute.
"""

from __future__ import annotations

from datetime import date

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.ops import report_cli
from tradehub_research.ops.common import ResearchPaths
from tradehub_research.ops.report_cli import build_daily_report
from tradehub_research.validation.experiment_db import ExperimentDB

REPORTING_DAY = date(2026, 7, 13)  # a Monday
EARLIER_DAY = date(2026, 7, 10)  # the previous Friday


def _fixed_clock(monkeypatch, day: date) -> None:
    """Pin the report's own day source (it owns the reporting day)."""

    class _Fixed(date):
        @classmethod
        def today(cls) -> _Fixed:
            return cls(day.year, day.month, day.day)

    monkeypatch.setattr(report_cli, "date", _Fixed)


def _paths(tmp_path) -> ResearchPaths:
    return ResearchPaths(
        research_dir=tmp_path / "research",
        research_db=tmp_path / "research.db",
        experiment_db=tmp_path / "experiment.db",
        replay_db=tmp_path / "replay.db",
        snapshots_dir=tmp_path / "snapshots",
        artifacts_dir=tmp_path / "artifacts",
        raw_cache=tmp_path / "raw",
    )


def _seed(tmp_path, monkeypatch):
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "S1",
                "TEST",
                "US",
                "Test Co",
                "Technology",
                "HW",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
    # The report reads the runner ledger; keep it isolated from the host.
    monkeypatch.setattr(report_cli, "LEDGER", tmp_path / "ledger.jsonl")
    exp = ExperimentDB(tmp_path / "experiment.db")
    exp.migrate()
    return _paths(tmp_path), research_db, exp


def _prediction(exp: ExperimentDB, prediction_id: str, provenance: str = "production") -> None:
    """Distinct variant per prediction: (security, as_of, variant, horizon) is UNIQUE."""
    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_prediction("
            "prediction_id, security_id, as_of, variant_name, score_value, state, "
            "screen_passed, sufficient_data, raw_features_hash, config_hash, "
            "evidence_ids_json, horizon_sessions, outcome_due_date, created_at, provenance"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                prediction_id,
                "S1",
                "2026-06-01",
                f"production/{prediction_id}",
                1.0,
                "ok",
                1,
                1,
                "rawhash",
                "confighash",
                "[]",
                21,
                "2026-07-01",
                "2026-06-01T20:15:00Z",
                provenance,
            ),
        )


def _outcome(exp: ExperimentDB, prediction_id: str, appended_at: str) -> None:
    """Append an outcome with a controlled durable creation timestamp."""
    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_outcome VALUES (?,?,?,?,?,?,?,?,?)",
            (
                f"outcome-{prediction_id}-{appended_at}",
                prediction_id,
                "OBSERVED",
                0.05,
                0.05,
                0.01,
                "2026-06-02",
                "2026-06-29",
                appended_at,
            ),
        )


def _report(settings, exp, paths) -> str:
    return build_daily_report(settings=settings, experiment_db=exp, paths=paths, analytics={})


def _new_matured(report: str) -> str:
    for line in report.splitlines():
        if line.startswith("New matured outcomes:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("the report no longer carries the new-matured line")


def test_zero_lifetime_outcomes_reports_zero(tmp_path, monkeypatch):
    paths, research_db, exp = _seed(tmp_path, monkeypatch)
    _prediction(exp, "pred-1")
    _fixed_clock(monkeypatch, REPORTING_DAY)

    report = _report(ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000), exp, paths)
    assert _new_matured(report) == "0"
    assert "Predictions: 1" in report


def test_historical_outcomes_are_not_new_today(tmp_path, monkeypatch):
    """Lifetime outcomes from earlier days must not render as new."""
    paths, research_db, exp = _seed(tmp_path, monkeypatch)
    for index in range(4):
        prediction_id = f"pred-{index}"
        _prediction(exp, prediction_id)
        _outcome(exp, prediction_id, f"{EARLIER_DAY.isoformat()}T15:45:00Z")
    _fixed_clock(monkeypatch, REPORTING_DAY)

    report = _report(ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000), exp, paths)
    assert _new_matured(report) == "0", "4 lifetime outcomes, none appended today"


def test_outcomes_appended_today_count(tmp_path, monkeypatch):
    paths, research_db, exp = _seed(tmp_path, monkeypatch)
    for index in range(4):
        prediction_id = f"pred-old-{index}"
        _prediction(exp, prediction_id)
        _outcome(exp, prediction_id, f"{EARLIER_DAY.isoformat()}T15:45:00Z")
    for index in range(3):
        prediction_id = f"pred-new-{index}"
        _prediction(exp, prediction_id)
        # 15:45Z == 23:45 local (+08): the maturation timer's real slot.
        _outcome(exp, prediction_id, f"{REPORTING_DAY.isoformat()}T15:45:00Z")
    _fixed_clock(monkeypatch, REPORTING_DAY)

    report = _report(ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000), exp, paths)
    assert _new_matured(report) == "3", "3 appended today, 4 historical"


def test_todays_outcomes_are_not_new_tomorrow(tmp_path, monkeypatch):
    paths, research_db, exp = _seed(tmp_path, monkeypatch)
    for index in range(3):
        prediction_id = f"pred-new-{index}"
        _prediction(exp, prediction_id)
        _outcome(exp, prediction_id, f"{REPORTING_DAY.isoformat()}T15:45:00Z")
    settings = ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)

    _fixed_clock(monkeypatch, REPORTING_DAY)
    assert _new_matured(_report(settings, exp, paths)) == "3"

    _fixed_clock(monkeypatch, date(2026, 7, 14))
    assert _new_matured(_report(settings, exp, paths)) == "0", "the same rows are old now"

    _fixed_clock(monkeypatch, REPORTING_DAY)
    assert _new_matured(_report(settings, exp, paths)) == "3", "same-day rerun is stable"


def test_only_production_outcomes_contribute(tmp_path, monkeypatch):
    paths, research_db, exp = _seed(tmp_path, monkeypatch)
    _prediction(exp, "pred-prod")
    _prediction(exp, "pred-replay", provenance="replay_bootstrap")
    _outcome(exp, "pred-prod", f"{REPORTING_DAY.isoformat()}T15:45:00Z")
    _outcome(exp, "pred-replay", f"{REPORTING_DAY.isoformat()}T15:45:00Z")
    _fixed_clock(monkeypatch, REPORTING_DAY)

    report = _report(ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000), exp, paths)
    assert _new_matured(report) == "1", "replay_bootstrap rows are not production"
    assert "Predictions: 1" in report, "and Predictions stays production-only"

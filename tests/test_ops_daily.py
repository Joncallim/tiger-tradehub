"""Ops smoke tests: deterministic cycle/capture/maturation/report contracts."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session
from tradehub_research.ops.forward_capture import capture_production_predictions
from tradehub_research.ops.health import forward_health
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.ops.report_cli import build_daily_report
from tradehub_research.screening import ScreeningConfig, run_screening
from tradehub_research.validation.experiment_db import ExperimentDB


def _seed(tmp_path: Path) -> tuple[ResearchPaths, ResearchDB, ExperimentDB]:
    """Minimal research db: one security with bars, one fact; empty ledger."""
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    store = EvidenceStore(research_db)
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
        conn.execute(
            "INSERT INTO universe_membership "
            "(security_id,price,market_cap,avg_dollar_volume,price_eligible,"
            "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,"
            "knowledge_time,pat_provenance,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "S1",
                None,
                None,
                None,
                0,
                0,
                0,
                1,
                "2026-01-01T00:00:00Z",
                None,
                "2026-01-01T00:00:00Z",
                "derived_from_index",
                None,
            ),
        )
    for day in ("2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28"):
        store.insert(
            security_id="S1",
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": "TEST",
                "session_date": day,
                "open": 50.0,
                "high": 51.0,
                "low": 49.0,
                "close": 50.5,
                "volume": 1000,
            },
            extraction_confidence=1.0,
            event_time=f"{day}T00:00:00Z",
            public_available_time=f"{day}T20:15:00Z",
            pat_provenance="derived_from_index",
            source_record_id=f"S1:{day}:bar",
        )
    exp = ExperimentDB(tmp_path / "experiment.db")
    exp.migrate()
    paths = ResearchPaths(
        research_dir=tmp_path / "research",
        research_db=research_db.path,
        experiment_db=exp.path,
        replay_db=tmp_path / "replay.db",
        snapshots_dir=tmp_path / "snapshots",
        artifacts_dir=tmp_path / "artifacts",
        raw_cache=tmp_path / "raw",
    )
    return paths, research_db, exp


def test_last_completed_session_clock():
    from datetime import datetime, timezone

    assert (
        last_completed_us_session(datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)).isoformat()
        == "2026-08-27"
    )
    assert (
        last_completed_us_session(datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)).isoformat()
        == "2026-08-28"
    )  # Monday -> Friday
    assert (
        last_completed_us_session(datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)).isoformat()
        == "2026-08-28"
    )  # Saturday -> Friday


def test_capture_records_genuine_production_predictions(tmp_path):
    paths, research_db, exp = _seed(tmp_path)
    settings = ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)
    run_id = run_screening("2026-08-28T20:15:00Z", None, ScreeningConfig(), database=research_db)
    summary = capture_production_predictions(
        settings=settings,
        experiment_db=exp,
        paths=paths,
        run_id=run_id,
        collection_date=date(2026, 8, 29),
    )
    assert summary["counts"]["production"] > 0
    assert summary["counts"]["rejected"] == 0
    # idempotent
    again = capture_production_predictions(
        settings=settings,
        experiment_db=exp,
        paths=paths,
        run_id=run_id,
        collection_date=date(2026, 8, 29),
    )
    assert again["counts"]["production"] == summary["counts"]["production"]
    with exp.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT as_of, provenance, COUNT(*) FROM forward_prediction GROUP BY as_of, provenance"
        ).fetchall()
    assert all(r["provenance"] == "production" for r in rows)
    assert all(r["as_of"] == "2026-08-28" for r in rows)  # actual screen date, never clamped


def test_maturation_honest_when_bars_missing(tmp_path):
    paths, research_db, exp = _seed(tmp_path)
    settings = ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)
    run_id = run_screening("2026-08-28T20:15:00Z", None, ScreeningConfig(), database=research_db)
    capture_production_predictions(
        settings=settings,
        experiment_db=exp,
        paths=paths,
        run_id=run_id,
        collection_date=date(2026, 8, 29),
    )
    # Nothing is due on the collection date (horizons are forward) -> honest
    # no-op; the maturation query is production-scoped.
    summary = mature_due_outcomes(
        settings=settings,
        experiment_db=exp,
        paths=paths,
        collection_date=date(2026, 8, 29),
    )
    assert summary["status"] == "OK"
    health = forward_health(experiment_db=exp, paths=paths, collection_date=date(2026, 8, 29))
    assert health["production_predictions"] > 0
    assert health["matured"] == {}  # nothing matured yet (horizons not due)


def test_daily_report_honest_when_broker_unavailable(tmp_path):
    paths, research_db, exp = _seed(tmp_path)
    settings = ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)
    report = build_daily_report(settings=settings, experiment_db=exp, paths=paths, analytics={})
    assert "Today      unavailable (unavailable)" in report
    assert "$0" not in report
    assert "No action required." in report

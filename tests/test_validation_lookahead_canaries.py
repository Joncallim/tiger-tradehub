from pathlib import Path

from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.lookahead_canaries import (
    run_adjusted_price_canary,
    run_runtime_canary,
    run_static_import_boundary_canary,
)


def test_runtime_canary_detects_no_future_pat_leak(tmp_path):
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    result = run_runtime_canary(research_db, experiment_db)

    assert result["canary_kind"] == "runtime_future_pat"
    assert result["detected"] == 0  # no leak: future-PAT evidence invisible


def test_adjusted_price_canary_detects_no_leak(tmp_path):
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    result = run_adjusted_price_canary(research_db, experiment_db)

    assert result["canary_kind"] == "adjusted_price_leak"
    assert result["detected"] == 0


def test_static_import_boundary_canary_clean(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    repo_root = Path(__file__).resolve().parent.parent
    result = run_static_import_boundary_canary(repo_root, experiment_db)

    assert result["canary_kind"] == "static_import_boundary"
    assert result["detected"] == 0, f"feature modules import outcome builder: {result['detail']}"


def test_canary_rows_are_append_only(tmp_path):
    import sqlite3

    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    run_runtime_canary(research_db, experiment_db)
    run_runtime_canary(research_db, experiment_db)

    with experiment_db.connect(read_only=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM lookahead_canary_run").fetchone()[0]
    assert count == 2  # both runs recorded; append-only

    try:
        with experiment_db.connect() as conn:
            conn.execute("DELETE FROM lookahead_canary_run")
        raise AssertionError("delete should have been blocked")
    except sqlite3.IntegrityError as exc:
        assert "append-only" in str(exc)

from pathlib import Path

from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.snapshot_builder import (
    build_validation_snapshot,
    load_dataset_snapshot,
)


def test_build_validation_snapshot_freezes_and_registers(tmp_path):
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    snapshot_id = build_validation_snapshot(
        research_db, experiment_db, dest_dir=tmp_path / "snapshots"
    )

    row = load_dataset_snapshot(experiment_db, snapshot_id)
    assert row["status"] == "READY"
    assert row["source_db_schema_version"] == research_db.schema_version()
    assert Path(row["artifact_path"]).exists()


def test_dataset_snapshot_records_coverage_summary(tmp_path):
    import json

    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    snapshot_id = build_validation_snapshot(
        research_db, experiment_db, dest_dir=tmp_path / "snapshots"
    )
    row = load_dataset_snapshot(experiment_db, snapshot_id)
    coverage = json.loads(row["coverage_summary_json"])
    assert coverage["overall_posture"] == "ZERO_EVALUABLE"


def test_dataset_snapshot_is_append_only_once_ready(tmp_path):
    import sqlite3

    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    snapshot_id = build_validation_snapshot(
        research_db, experiment_db, dest_dir=tmp_path / "snapshots"
    )

    try:
        with experiment_db.connect() as conn:
            conn.execute(
                "UPDATE dataset_snapshot SET status='PENDING' WHERE snapshot_id=?",
                (snapshot_id,),
            )
        raise AssertionError("expected append-only trigger to abort the update")
    except sqlite3.IntegrityError as exc:
        assert "append-only" in str(exc)

"""Packet A: freeze the first validation snapshot.

Calls the existing tradehub_research.snapshot.create_snapshot() unmodified
(reuse, not reinvent), then records the resulting snapshot into
experiment.db's dataset_snapshot table together with a coverage-audit
summary and, when a hash-selected universe sample was used, the
universe_sample_id.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.snapshot import create_snapshot
from tradehub_research.validation.coverage_audit import run_coverage_audit


def _current_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def build_validation_snapshot(
    research_db: ResearchDB,
    experiment_db: ResearchDB,
    *,
    dest_dir: Path,
    scope: str = "phase-5 validation snapshot",
    universe_sample_id: str | None = None,
) -> str:
    """Freeze a research.db snapshot and register it in experiment.db.

    Returns the dataset_snapshot_id (== the underlying snapshot.py
    content-hash-verified snapshot_id).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    coverage = run_coverage_audit(database=research_db)

    dataset_snapshot_id = str(uuid.uuid4())
    dest_path = dest_dir / f"{dataset_snapshot_id}.sqlite"
    underlying_snapshot_id = create_snapshot(research_db, dest_path, scope=scope)

    # snapshot.py generates its own internal snapshot_id (a uuid distinct from
    # dataset_snapshot_id) -- both are recorded for traceability, but
    # dataset_snapshot_id is the experiment.db-side identifier used by
    # downstream evaluation_regime/experiment_attempt rows.
    manifest = {
        "underlying_snapshot_id": underlying_snapshot_id,
        "source_commit": _current_commit(),
        "source_db_schema_version": research_db.schema_version(),
        "artifact_path": str(dest_path),
        "scope": scope,
    }
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    import hashlib

    manifest_hash = hashlib.sha256(manifest_json.encode()).hexdigest()

    with dest_path.open("rb") as handle:
        artifact_content_hash = hashlib.sha256(handle.read()).hexdigest()

    coverage_summary_json = json.dumps(coverage, sort_keys=True, separators=(",", ":"))

    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot "
            "(snapshot_id,source_commit,source_db_schema_version,universe_sample_id,"
            "manifest_json,manifest_hash,artifact_path,artifact_content_hash,"
            "coverage_summary_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                dataset_snapshot_id,
                manifest["source_commit"],
                manifest["source_db_schema_version"],
                universe_sample_id,
                manifest_json,
                manifest_hash,
                str(dest_path),
                artifact_content_hash,
                coverage_summary_json,
                "READY",
                utc_now(),
            ),
        )
    return dataset_snapshot_id


def load_dataset_snapshot(experiment_db: ResearchDB, dataset_snapshot_id: str) -> dict[str, Any]:
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM dataset_snapshot WHERE snapshot_id=?", (dataset_snapshot_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown dataset_snapshot: {dataset_snapshot_id}")
    return dict(row)

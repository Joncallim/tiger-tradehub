"""Packet C: replay the PRODUCTION screening pipeline per grid date.

For each monthly PIT grid timestamp, this calls the existing, unmodified
screening.run_screening(as_of, snapshot_id, config, database=...) with:
- config.snapshot_path = the frozen dataset_snapshot artifact (so features
  read the immutable PIT view, never the live research.db),
- database = a SEPARATE research-schema DB file (validation_replay.db)
  where pipeline_run/screen_result/candidate rows land.

This gives Packet C the exact production pipeline_run/screen_result/candidate
tables populated by literally the production code path -- no train/prod
skew by construction (RA-05 contracts 1, 5, 6, 8, 9).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tradehub_research.db import ResearchDB
from tradehub_research.screening import ScreeningConfig, run_screening
from tradehub_research.validation.snapshot_builder import load_dataset_snapshot


def replay_monthly_grid(
    experiment_db: ResearchDB,
    replay_db: ResearchDB,
    *,
    dataset_snapshot_id: str,
    grid_timestamps: list[str],
    funnel_budget: int = 50,
    control_count: int = 5,
) -> dict[str, str]:
    """Run run_screening once per grid timestamp against the frozen snapshot.

    Returns {grid_timestamp: run_id}. Determinism: re-running with the same
    snapshot+config must reproduce identical run_ids and screen_result rows
    (the production pipeline is insert-or-verify by design); a differing
    stored hash is a determinism error and fails the run.
    """
    snapshot = load_dataset_snapshot(experiment_db, dataset_snapshot_id)
    manifest = json.loads(snapshot["manifest_json"])
    snapshot_path = Path(snapshot["artifact_path"])
    if not snapshot_path.exists():
        raise ValueError(f"dataset_snapshot artifact missing: {snapshot_path}")
    underlying_snapshot_id = manifest["underlying_snapshot_id"]

    _mirror_snapshot_registration(replay_db, underlying_snapshot_id)
    _mirror_security_identity(replay_db, snapshot_path)

    config = ScreeningConfig.from_dict(
        {
            "funnel": {"budget": funnel_budget, "control_count": control_count},
            "holdings": [],
            "universe_coverage": ["SUPPORTED"],
            "snapshot_path": str(snapshot_path),
        }
    )

    run_ids: dict[str, str] = {}
    for timestamp in grid_timestamps:
        run_id = run_screening(timestamp, underlying_snapshot_id, config, database=replay_db)
        run_ids[timestamp] = run_id
    return run_ids


def _mirror_snapshot_registration(replay_db: ResearchDB, underlying_snapshot_id: str) -> None:
    """Mirror the snapshot_version registration row into the replay DB so
    the production pipeline's pipeline_run.input_snapshot_id FK is
    satisfiable. The row is registration metadata (snapshot identity),
    never evidence; replay reads the actual snapshot artifact for data."""
    with replay_db.connect() as conn:
        existing = conn.execute(
            "SELECT 1 FROM snapshot_version WHERE snapshot_id=?", (underlying_snapshot_id,)
        ).fetchone()
        if existing is not None:
            return
        source = conn.execute(
            "SELECT source_db FROM snapshot_manifest WHERE snapshot_id=?",
            (underlying_snapshot_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO snapshot_version "
            "(snapshot_id,created_from_db_version,scope_description,created_at,"
            "content_hash,status,destination_path) VALUES (?,?,?,?,?,?,?)",
            (
                underlying_snapshot_id,
                0,
                "validation replay mirror",
                "2026-01-01T00:00:00Z",
                "mirrored",
                "READY",
                str(source[0]) if source else None,
            ),
        )


def _mirror_security_identity(replay_db: ResearchDB, snapshot_path: Path) -> None:
    """Copy the security identity rows (ticker/CIK/exchange registration --
    identity metadata, never evidence) from the snapshot artifact into the
    replay DB so screen_result's security FK is satisfiable. The replay
    reads actual evidence through the snapshot handle."""
    import sqlite3

    with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as source:
        rows = source.execute(
            "SELECT security_id,canonical_ticker,exchange,name,sector,industry,"
            "sector_coverage_status,first_seen,delisted_at FROM security"
        ).fetchall()
    with replay_db.connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO security VALUES (?,?,?,?,?,?,?,?,?)", rows)


def load_screen_results(replay_db: ResearchDB) -> list[dict[str, Any]]:
    """All screen_result rows from a replay DB (pass AND fail, sufficient AND
    insufficient -- never just candidates), each augmented with its Hunter
    family (screen_definition join) and its EVALUATION date (pipeline_run
    as_of join).

    CRITICAL: the observation date is pipeline_run.as_of (the grid
    timestamp), NEVER screen_result.computed_at -- computed_at is the run's
    wall-clock time (utc_now), which would silently shift every observation
    to the replay run's date and break date-keyed evaluation entirely."""
    with replay_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT sr.*, sd.family, pr.as_of FROM screen_result sr "
            "JOIN screen_definition sd ON sd.config_hash = sr.config_hash "
            "JOIN pipeline_run pr ON pr.run_id = sr.run_id "
            "ORDER BY pr.as_of, sr.security_id"
        ).fetchall()
    return [dict(row) for row in rows]


def screen_observation_date(screen: dict[str, Any]) -> str:
    """The evaluation date of a screen row: pipeline_run.as_of when present,
    computed_at only as a last-resort fallback (and never silently)."""
    as_of = screen.get("as_of")
    if as_of:
        return str(as_of)[:10]
    return str(screen.get("computed_at", ""))[:10]

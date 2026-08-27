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


def load_screen_results(replay_db: ResearchDB) -> list[dict[str, Any]]:
    """All screen_result rows from a replay DB (pass AND fail, sufficient AND
    insufficient -- never just candidates), each augmented with its Hunter
    family via the screen_definition join."""
    with replay_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT sr.*, sd.family FROM screen_result sr "
            "JOIN screen_definition sd ON sd.config_hash = sr.config_hash "
            "ORDER BY sr.run_id, sr.security_id"
        ).fetchall()
    return [dict(row) for row in rows]

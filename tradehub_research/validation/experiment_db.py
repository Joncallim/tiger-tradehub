"""ExperimentDB -- the append-only validation-engine storage boundary.

A thin construction of ResearchDB pointed at experiment.db with
VALIDATION_MIGRATIONS. This is the ONE physical SQLite file that holds all
Phase 5 experiment governance/results (dataset_snapshot, evaluation_regime,
experiment_attempt, metric, benchmark_artifact, outcome_label,
lookahead_canary_run, backfill_attempt, universe_sample). research.db is
never written by anything in tradehub_research/validation or
tradehub_research/backfill.
"""

from __future__ import annotations

from pathlib import Path

from tradehub_research.db import ResearchDB
from tradehub_research.validation.validation_schema import (
    VALIDATION_MIGRATIONS,
    VALIDATION_SCHEMA_VERSION,
)

DEFAULT_EXPERIMENT_DB_PATH = Path("data/research/experiment.db")


def ExperimentDB(
    path: Path | str = DEFAULT_EXPERIMENT_DB_PATH, busy_timeout_ms: int = 5000
) -> ResearchDB:
    """Return a ResearchDB instance bound to the experiment.db schema."""
    return ResearchDB(
        path,
        busy_timeout_ms,
        migrations=VALIDATION_MIGRATIONS,
        expected_schema_version=VALIDATION_SCHEMA_VERSION,
    )

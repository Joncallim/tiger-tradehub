"""Append-only experiment attempt + metric writers (Packet D governance).

Every evaluation -- baseline, hunter eval, ablation, walk-forward fold,
holdout -- records ONE experiment_attempt row (immutable logical inputs,
append-only) plus its metric rows. A failed/unflattering attempt remains
visible forever (RA-05 16); there is no delete path and no overwrite path.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from tradehub_research.db import ResearchDB, utc_now


def start_attempt(
    experiment_db: ResearchDB,
    *,
    regime_id: str,
    dataset_snapshot_id: str,
    variant_kind: str,
    variant_name: str,
    config: dict[str, Any],
    fold_id: str | None = None,
    horizon_sessions: int | None = None,
    attempt_number: int = 1,
) -> str:
    """Insert a RUNNING attempt; returns attempt_id."""
    config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
    config_hash = hashlib.sha256(config_json.encode()).hexdigest()
    attempt_id = str(uuid.uuid4())
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO experiment_attempt "
            "(attempt_id,regime_id,dataset_snapshot_id,variant_kind,variant_name,"
            "config_json,config_hash,fold_id,horizon_sessions,attempt_number,status,"
            "failure_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                attempt_id,
                regime_id,
                dataset_snapshot_id,
                variant_kind,
                variant_name,
                config_json,
                config_hash,
                fold_id,
                horizon_sessions,
                attempt_number,
                "RUNNING",
                None,
                utc_now(),
            ),
        )
    return attempt_id


def complete_attempt(
    experiment_db: ResearchDB, attempt_id: str, *, status: str = "COMPLETE"
) -> None:
    if status not in {"COMPLETE", "FAILED", "INSUFFICIENT_DATA"}:
        raise ValueError(f"invalid terminal attempt status: {status}")
    with experiment_db.connect() as conn:
        conn.execute(
            "UPDATE experiment_attempt SET status=?, finished_at=? WHERE attempt_id=?",
            (status, utc_now(), attempt_id),
        )


def record_metric(
    experiment_db: ResearchDB,
    *,
    attempt_id: str,
    horizon_sessions: int,
    segment: str,
    metric_name: str,
    point_estimate: float,
    ci_lower: float | None = None,
    ci_upper: float | None = None,
    bootstrap_seed: int | None = None,
    bootstrap_method: str | None = None,
    date_count: int | None = None,
    security_count: int | None = None,
    effective_n: float | None = None,
    low_confidence: bool = False,
) -> str:
    metric_id = str(uuid.uuid4())
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO metric VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                metric_id,
                attempt_id,
                horizon_sessions,
                segment,
                metric_name,
                point_estimate,
                ci_lower,
                ci_upper,
                bootstrap_seed,
                bootstrap_method,
                date_count,
                security_count,
                effective_n,
                int(low_confidence),
                utc_now(),
            ),
        )
    return metric_id


def variant_count(experiment_db: ResearchDB, regime_id: str) -> int:
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM experiment_attempt WHERE regime_id=?", (regime_id,)
        ).fetchone()
    return int(row[0])


def attempts_by_status(experiment_db: ResearchDB, regime_id: str) -> dict[str, int]:
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM experiment_attempt WHERE regime_id=? GROUP BY status",
            (regime_id,),
        ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}

from __future__ import annotations

import json
import uuid
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts, utc_now


class ExperimentRegistry:
    def __init__(self, database: ResearchDB):
        self.database = database

    def start(
        self,
        name: str,
        config: dict[str, Any],
        input_hash: str,
        *,
        scoring_version: str | None = None,
        input_snapshot_id: str | None = None,
        evaluation_window_start: str | None = None,
        evaluation_window_end: str | None = None,
        status: str = "STARTED",
    ) -> str:
        experiment_id = str(uuid.uuid4())
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO experiment_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    experiment_id,
                    name,
                    json.dumps(config, sort_keys=True),
                    scoring_version,
                    input_snapshot_id,
                    input_hash,
                    normalize_ts(evaluation_window_start) if evaluation_window_start else None,
                    normalize_ts(evaluation_window_end) if evaluation_window_end else None,
                    status,
                    None,
                    utc_now(),
                    None,
                ),
            )
        return experiment_id

    def record_attempt(
        self,
        experiment_id: str,
        attempt_number: int,
        status: str,
        result_reference: str | None = None,
    ) -> None:
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO oos_evaluation_log("
                "experiment_id,attempt_number,status,result_reference,recorded_at) "
                "VALUES (?,?,?,?,?)",
                (experiment_id, attempt_number, status, result_reference, utc_now()),
            )

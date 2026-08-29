"""Daily forward-learning capture (issue #39 B2).

After a production research cycle actually screens the population, the
genuine forward predictions are recorded: every screened security
(PASS/FAIL/insufficient), provenance='production', as_of = the ACTUAL
screen date (the last completed US session). The future-dated guard in
record_prediction() structurally rejects anything else.

Idempotent: re-running the capture dedupes on (security, as_of, variant,
horizon); the first-recorded row is immutable.
"""

from __future__ import annotations

import json
import sys

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.screen_store import ScreenStore
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import (
    HORIZON_SESSIONS,
    record_prediction_from_screen,
)


def capture_production_predictions(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    run_id: str | None = None,
    collection_date=None,
) -> dict:
    """Record production forward predictions for the most recent cycle(s).

    ``run_id``: capture a specific cycle; default = the latest completed run
    that has not yet been captured (dedupe makes re-capture a no-op).
    """
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    store = ScreenStore(research_db)

    if run_id is None:
        with research_db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT run_id FROM pipeline_run ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return {"status": "NO_RUN", "predictions": 0}
        run_id = str(row[0])

    # The PRODUCTION observation date is the pipeline_run.as_of (the actual
    # screening date) -- NEVER screen_result.computed_at (wall clock).
    with research_db.connect(read_only=True) as conn:
        run_row = conn.execute(
            "SELECT as_of FROM pipeline_run WHERE run_id=?", (run_id,)
        ).fetchone()
    if run_row is None:
        return {"status": "NO_RUN", "predictions": 0}
    observation_ts = str(run_row["as_of"])
    observation_ts[:10]

    screens = store.load_results_for_funnel(run_id)
    for screen in screens:
        screen["computed_at"] = observation_ts  # observation date for the guard
    # Only ACTUAL production screens: the observation date must be the last
    # completed session (or earlier); the guard rejects anything future.
    counts = {"production": 0, "replay_bootstrap": 0, "rejected": 0}
    for screen in screens:
        for horizon in HORIZON_SESSIONS:
            try:
                record_prediction_from_screen(
                    experiment_db,
                    screen=screen,
                    horizon_sessions=horizon,
                    variant_name=f"production/{screen.get('family', 'unknown')}",
                    collection_date=collection_date,
                    provenance="production",
                )
                counts["production"] += 1
            except ValueError as exc:
                if "future-dated" in str(exc):
                    counts["rejected"] += 1
                else:
                    raise
    return {
        "status": "OK",
        "run_id": run_id,
        "screens": len(screens),
        "horizons": list(HORIZON_SESSIONS),
        "counts": counts,
        "created_at": utc_now(),
    }


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    experiment_db = ExperimentDB(research_paths().experiment_db)
    summary = capture_production_predictions(settings=settings, experiment_db=experiment_db)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

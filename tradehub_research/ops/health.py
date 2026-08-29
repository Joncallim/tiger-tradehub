"""Forward-data health + data-freshness health (issue #39 B7).

Operational health figures -- NOT alpha claims. Both functions are
deterministic and read-only.
"""

from __future__ import annotations

import json
import sys

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session, research_paths
from tradehub_research.validation.experiment_db import ExperimentDB


def forward_health(
    *,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    collection_date=None,
) -> dict:
    """Production forward-ledger health (provenance='production' only)."""
    paths = paths or research_paths()
    due = (collection_date or last_completed_us_session()).isoformat()
    with experiment_db.connect(read_only=True) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM forward_prediction WHERE provenance='production'"
        ).fetchone()[0]
        by_horizon = conn.execute(
            "SELECT horizon_sessions, COUNT(*) FROM forward_prediction "
            "WHERE provenance='production' GROUP BY horizon_sessions ORDER BY horizon_sessions"
        ).fetchall()
        due_count = conn.execute(
            "SELECT COUNT(*) FROM forward_prediction WHERE provenance='production' "
            "AND outcome_due_date <= ? AND NOT EXISTS "
            "(SELECT 1 FROM forward_outcome o "
            " WHERE o.prediction_id=forward_prediction.prediction_id)",
            (due,),
        ).fetchone()[0]
        matured = conn.execute(
            "SELECT outcome_status, COUNT(*) FROM forward_outcome o "
            "JOIN forward_prediction p ON p.prediction_id=o.prediction_id "
            "WHERE p.provenance='production' GROUP BY outcome_status"
        ).fetchall()
        last_screen = conn.execute(
            "SELECT MAX(as_of) FROM forward_prediction WHERE provenance='production'"
        ).fetchone()[0]
    return {
        "production_predictions": total,
        "by_horizon": {str(r[0]): r[1] for r in by_horizon},
        "predictions_due": due_count,
        "matured": {str(r[0]): r[1] for r in matured},
        "last_production_screen": last_screen,
        "generated_at": utc_now(),
    }


def refresh_health(
    *,
    settings: ResearchSettings,
    paths: ResearchPaths | None = None,
    as_of=None,
) -> dict:
    """Market-data freshness: last bar date per cohort security vs the last
    completed US session; stale names listed honestly (never backfilled)."""
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    as_of = (as_of or last_completed_us_session()).isoformat()
    with research_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT s.security_id, s.canonical_ticker, "
            "(SELECT MAX(json_extract(structured_fields, '$.session_date')) "
            "  FROM evidence_event e WHERE e.security_id=s.security_id "
            "  AND e.source_id='tiingo_eod') AS last_bar "
            "FROM security s "
            "JOIN universe_membership m ON m.security_id=s.security_id "
            "WHERE m.eligible=1 AND NOT EXISTS (SELECT 1 FROM universe_membership s2 "
            "  WHERE s2.supersedes_id=m.id)"
        ).fetchall()
    with_data = [r for r in rows if r["last_bar"]]
    stale = [
        {"ticker": r["canonical_ticker"], "last_bar": r["last_bar"]}
        for r in with_data
        if r["last_bar"] < as_of
    ]
    return {
        "as_of": as_of,
        "securities_expected": len(rows),
        "with_bars": len(with_data),
        "without_bars": len(rows) - len(with_data),
        "stale": stale[:20],
        "stale_count": len(stale),
        "fresh": len(with_data) - len(stale),
        "generated_at": utc_now(),
    }


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    exp = ExperimentDB(research_paths().experiment_db)
    out = {
        "forward": forward_health(experiment_db=exp),
        "refresh": refresh_health(settings=settings),
    }
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

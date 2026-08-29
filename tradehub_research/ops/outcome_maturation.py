"""Daily outcome maturation (issue #39 B2).

For production predictions whose horizon is DUE (outcome_due_date <=
collection date) and which have no outcome yet, build the realized outcome
from the research DB bars and APPEND a forward_outcome row. The prediction
row is NEVER modified (append-only by trigger). Immature horizons stay
pending; unavailable names classify honestly
(DELISTING_OUTCOME_UNKNOWN / CENSORED_INSUFFICIENT_HORIZON).
"""

from __future__ import annotations

import json
import sys
from datetime import date

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import append_outcome


def _realized_return(
    research_db: ResearchDB, security_id: str, entry_date: str, horizon_days: int
) -> tuple[float | None, str]:
    """Total return over (entry_date, entry_date + horizon_days] using the
    research DB bars. (entry, exit] convention; None entry/exit -> honest
    status."""
    with research_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT json_extract(structured_fields, '$.session_date') AS d, "
            "json_extract(structured_fields, '$.close') AS c "
            "FROM evidence_event WHERE security_id=? AND source_id='tiingo_eod' "
            "AND json_extract(structured_fields, '$.record_type')='price_bar' "
            "AND json_extract(structured_fields, '$.session_date') > ? "
            "ORDER BY d",
            (security_id, entry_date),
        ).fetchall()
    if not rows:
        return None, "ENTRY_UNAVAILABLE"
    first_close = rows[0]["c"]
    if first_close is None:
        return None, "ENTRY_UNAVAILABLE"
    # Exit: the bar nearest to (but not beyond) entry + horizon_days.
    target = date.fromisoformat(entry_date)
    from datetime import timedelta

    horizon_end = (target + timedelta(days=horizon_days)).isoformat()
    exit_close = None
    for row in rows:
        if row["d"] <= horizon_end:
            exit_close = row["c"]
        else:
            break
    if exit_close is None:
        return None, "CENSORED_INSUFFICIENT_HORIZON"
    try:
        return float(exit_close) / float(first_close) - 1.0, "OBSERVED"
    except (TypeError, ValueError, ZeroDivisionError):
        return None, "CENSORED_INSUFFICIENT_HORIZON"


def mature_due_outcomes(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    collection_date: date | None = None,
    horizon_calendar_days: dict[int, int] | None = None,
) -> dict:
    """Append outcomes for production predictions due at collection_date."""
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    due = collection_date or date.fromisoformat(utc_now()[:10])
    horizon_days = horizon_calendar_days or {21: 40, 63: 105, 126: 210, 252: 420}

    with experiment_db.connect(read_only=True) as conn:
        pending = conn.execute(
            "SELECT p.prediction_id, p.security_id, p.as_of, p.horizon_sessions "
            "FROM forward_prediction p "
            "WHERE p.provenance='production' AND p.outcome_due_date <= ? "
            "AND NOT EXISTS (SELECT 1 FROM forward_outcome o "
            "WHERE o.prediction_id=p.prediction_id)",
            (due.isoformat(),),
        ).fetchall()

    matured = {"OBSERVED": 0, "ENTRY_UNAVAILABLE": 0, "CENSORED_INSUFFICIENT_HORIZON": 0}
    for row in pending:
        ret, status = _realized_return(
            research_db,
            str(row["security_id"]),
            str(row["as_of"]),
            horizon_days.get(int(row["horizon_sessions"]), 400),
        )
        append_outcome(
            experiment_db,
            prediction_id=str(row["prediction_id"]),
            outcome_status=status,
            total_return=ret,
        )
        matured[status] = matured.get(status, 0) + 1

    return {
        "status": "OK",
        "collection_date": due.isoformat(),
        "due": len(pending),
        "matured": matured,
        "created_at": utc_now(),
    }


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    experiment_db = ExperimentDB(research_paths().experiment_db)
    summary = mature_due_outcomes(settings=settings, experiment_db=experiment_db)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

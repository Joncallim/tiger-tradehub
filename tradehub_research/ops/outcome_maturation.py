"""Daily outcome maturation (issue #39 B2).

For production predictions whose horizon is DUE (``outcome_due_date <= collection
date``) and which have no outcome yet, build the realized outcome from the
research DB bars and APPEND a ``forward_outcome`` row. The prediction row is
NEVER modified (append-only by trigger).

HORIZON SEMANTICS (fixed 2026-09-22, before the first production cohort matured)

A horizon is a number of COMPLETED TRADING SESSIONS after the entry session --
see ``validation/horizons.py`` for the one authoritative definition. This module
used to carry its own ``{21: 40, 63: 105, 126: 210, 252: 420}`` calendar-day map
and then take the LATEST bar at or before that target as the exit, which could

  * append ``OBSERVED`` before the session horizon had completed at all, and
  * observe a horizon far longer than the contract when the run was late.

Now the exit is exactly the horizon-th session after entry, and a shortfall of
realized sessions is never reported as an observation.

HONEST STATES

The outcome table is append-only with ``UNIQUE(prediction_id)``: ONE outcome per
prediction, forever. A wrong terminal classification therefore cannot be undone,
so non-OBSERVED labels are only emitted when the data can no longer improve:

  * fewer realized sessions than the horizon, or the exit session is still in the
    future, or the bars are merely lagging -> **pending** (no row); the next run
    re-evaluates;
  * the security is resolvable but its data stopped well before the required exit
    session -> ``CENSORED_INSUFFICIENT_HORIZON``;
  * the security is delisted / no longer resolvable -> ``DELISTING_OUTCOME_UNKNOWN``;
  * a realized bar carries an unusable price -> ``CENSORED_INSUFFICIENT_HORIZON``.

``mature_due_outcomes`` never passes a status outside the schema's enum (the
pre-fix path could return ``ENTRY_UNAVAILABLE``, which the CHECK constraint
rejects -- an insert that would abort the whole run).
"""

from __future__ import annotations

import json
import sys
from datetime import date

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.market_calendar import count_sessions
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import append_outcome
from tradehub_research.validation.horizons import (
    HORIZON_SESSIONS,
    required_exit_session,
    select_exit_bar,
)

#: Sessions of grace before a missing exit bar is treated as permanently missing.
#: The nightly refresh ingests a rolling window, so a bar a few sessions late is
#: normal; because a censored row can never become OBSERVED, the classification
#: waits until the data genuinely cannot arrive.
DATA_GRACE_SESSIONS = 5

#: Statuses the ``forward_outcome`` CHECK constraint accepts.
TERMINAL_STATUSES = ("OBSERVED", "DELISTING_OUTCOME_UNKNOWN", "CENSORED_INSUFFICIENT_HORIZON")

#: Pending (no row appended) reasons. Pending is retryable by design.
AWAITING_ENTRY = "AWAITING_ENTRY"
AWAITING_HORIZON = "AWAITING_HORIZON"


def _session_bars(
    research_db: ResearchDB, security_id: str, as_of: str
) -> list[tuple[str, float | None]]:
    """Canonical session bars strictly after ``as_of``: [(session_date, close)].

    One entry per session, oldest first. Sessions -- not bars -- are the unit:
    duplicate rows for a session collapse, and a session with no bar simply is
    not a completed session yet.
    """
    with research_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT json_extract(structured_fields, '$.session_date') AS d, "
            "json_extract(structured_fields, '$.close') AS c "
            "FROM evidence_event WHERE security_id=? AND source_id='tiingo_eod' "
            "AND json_extract(structured_fields, '$.record_type')='price_bar' "
            "AND json_extract(structured_fields, '$.session_date') > ? "
            "ORDER BY d",
            (security_id, as_of[:10]),
        ).fetchall()
    bars: list[tuple[str, float | None]] = []
    for row in rows:
        session = row["d"]
        if not session:
            continue
        session = str(session)[:10]
        if bars and bars[-1][0] == session:
            continue  # one canonical bar per session
        close = row["c"]
        try:
            close = float(close) if close is not None else None
        except (TypeError, ValueError):
            close = None
        bars.append((session, close))
    return bars


def _security_state(research_db: ResearchDB, security_id: str) -> tuple[bool, str | None]:
    """``(exists, delisted_at)`` for a security.

    A security that is absent from the table, or carries a ``delisted_at``, can
    never resolve a horizon: its outcome is unknown, which is a terminal and
    honest label -- not a reason to wait.
    """
    with research_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT delisted_at FROM security WHERE security_id=?", (security_id,)
        ).fetchone()
    if row is None:
        return False, None
    delisted_at = row["delisted_at"]
    return True, (str(delisted_at)[:10] if delisted_at else None)


def _evaluate(
    research_db: ResearchDB,
    *,
    security_id: str,
    as_of: str,
    horizon_sessions: int,
    collection_date: date,
) -> dict:
    """Classify one due prediction against its realized sessions.

    Returns ``{"status": <enum or None>, "pending": <reason or None>,
    "total_return": float | None, "entry_session_date": str | None,
    "exit_session_date": str | None, "detail": str}``.
    """
    exists, delisted_at = _security_state(research_db, security_id)
    bars = _session_bars(research_db, security_id, as_of)
    if not bars:
        # No entry session at all. Terminal only when the name cannot resolve.
        if not exists or delisted_at is not None:
            return {
                "status": "DELISTING_OUTCOME_UNKNOWN",
                "pending": None,
                "detail": "no entry session; security delisted or unknown",
            }
        return {
            "status": None,
            "pending": AWAITING_ENTRY,
            "detail": "no session after as_of yet",
        }

    entry_session, entry_close = bars[0]
    # The horizon counts sessions AFTER the entry session -- the same bars the
    # research builder feeds to its exit rule (bars strictly after entry).
    post_entry = bars[1:]
    exit_bar = select_exit_bar(post_entry, horizon_sessions)
    exit_session = required_exit_session(entry_session, horizon_sessions)

    # A delisting at or before the exit session means the horizon can never be
    # observed: never dropped, never imputed zero (the research contract).
    if delisted_at is not None and delisted_at <= exit_session:
        return {
            "status": "DELISTING_OUTCOME_UNKNOWN",
            "pending": None,
            "detail": f"delisted {delisted_at} at/before the exit session {exit_session}",
        }

    if exit_bar is None:
        # The horizon has not completed in sessions. Never OBSERVED on fewer.
        if collection_date < date.fromisoformat(exit_session):
            return {
                "status": None,
                "pending": AWAITING_HORIZON,
                "detail": f"exit session {exit_session} has not arrived",
            }
        lag = count_sessions(date.fromisoformat(bars[-1][0]), date.fromisoformat(exit_session))
        if lag <= DATA_GRACE_SESSIONS:
            return {
                "status": None,
                "pending": AWAITING_HORIZON,
                "detail": (
                    f"{len(bars)}/{horizon_sessions} sessions; data {lag} session(s) "
                    "behind the exit session"
                ),
            }
        return {
            "status": "CENSORED_INSUFFICIENT_HORIZON",
            "pending": None,
            "detail": (
                f"{len(bars)}/{horizon_sessions} sessions, exit session {exit_session} "
                f"is {lag} sessions past and the data did not arrive"
            ),
        }

    _exit_session, exit_close = exit_bar
    if entry_close is None or entry_close == 0 or exit_close is None:
        return {
            "status": "CENSORED_INSUFFICIENT_HORIZON",
            "pending": None,
            "detail": "a realized bar carries no usable close",
        }
    return {
        "status": "OBSERVED",
        "pending": None,
        "total_return": exit_close / entry_close - 1.0,
        "entry_session_date": entry_session,
        "exit_session_date": exit_session,
        "detail": f"{horizon_sessions} sessions {entry_session} -> {exit_session}",
    }


def mature_due_outcomes(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    collection_date: date | None = None,
) -> dict:
    """Append outcomes for production predictions due at collection_date.

    ``outcome_due_date`` is the SCHEDULING gate (an advisory calendar date, and
    for pre-fix rows the legacy approximation). Whether a horizon has matured is
    verified separately, from the realized sessions.
    """
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    due = collection_date or date.fromisoformat(utc_now()[:10])

    with experiment_db.connect(read_only=True) as conn:
        pending = conn.execute(
            "SELECT p.prediction_id, p.security_id, p.as_of, p.horizon_sessions "
            "FROM forward_prediction p "
            "WHERE p.provenance='production' AND p.outcome_due_date <= ? "
            "AND NOT EXISTS (SELECT 1 FROM forward_outcome o "
            "WHERE o.prediction_id=p.prediction_id)",
            (due.isoformat(),),
        ).fetchall()

    counts = {
        **{status: 0 for status in TERMINAL_STATUSES},
        AWAITING_ENTRY: 0,
        AWAITING_HORIZON: 0,
    }
    materialized = 0
    for row in pending:
        horizon = int(row["horizon_sessions"])
        if horizon not in HORIZON_SESSIONS:
            counts.setdefault("UNKNOWN_HORIZON", 0)
            counts["UNKNOWN_HORIZON"] += 1
            continue
        result = _evaluate(
            research_db,
            security_id=str(row["security_id"]),
            as_of=str(row["as_of"]),
            horizon_sessions=horizon,
            collection_date=due,
        )
        if result["status"] is None:
            counts[result["pending"]] = counts.get(result["pending"], 0) + 1
            continue
        append_outcome(
            experiment_db,
            prediction_id=str(row["prediction_id"]),
            outcome_status=result["status"],
            total_return=result.get("total_return"),
            entry_session_date=result.get("entry_session_date"),
            exit_session_date=result.get("exit_session_date"),
        )
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        materialized += 1

    return {
        "status": "OK",
        "collection_date": due.isoformat(),
        "due": len(pending),
        "materialized": materialized,
        "matured": counts,
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

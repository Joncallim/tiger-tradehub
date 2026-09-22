"""Forward-data health + data-freshness health (issue #39 B7).

Operational health figures -- NOT alpha claims. Both functions are
deterministic and read-only.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta, timezone

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session, research_paths
from tradehub_research.ops.market_calendar import count_sessions
from tradehub_research.validation.experiment_db import ExperimentDB


def _local_day_utc_bounds(day: date) -> tuple[str, str]:
    """UTC ISO bounds (``...Z``) of a LOCAL calendar day.

    ``appended_at`` is stored UTC, but "new today" is a statement about the
    operator's day -- the same clock the report's own day comes from. Half-open
    [start, end) so a row lands in exactly one day regardless of offset.
    """

    def _fmt(value: datetime) -> str:
        return (
            value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )

    start = datetime.combine(day, time.min).astimezone()
    return _fmt(start), _fmt(start + timedelta(days=1))


def _awaiting_entry_bar(
    experiment_db: ExperimentDB, research_db: ResearchDB, collection_date: date
) -> dict:
    """Due predictions whose expected ENTRY-SESSION bar is not available.

    These stay PENDING by design and are NEVER terminalized on a timeout:
    ``forward_outcome`` is append-only with one row per prediction, so an early
    ``CENSORED``/``DELISTING`` label would make a data-quality gap permanently
    irreversible, while a later ingest can still repair a missing bar. This block
    exists so the gap is visible, aged and actionable instead.

    Each affected security is classified as still recoverable, or as retired /
    delisted / unknown -- positive evidence that the entry cannot be recovered.
    """
    from tradehub_research.ops.daily_refresh import retired_tickers
    from tradehub_research.ops.outcome_maturation import AWAITING_ENTRY_BAR, _evaluate

    with experiment_db.connect(read_only=True) as conn:
        pending = conn.execute(
            "SELECT p.prediction_id, p.security_id, p.as_of, p.horizon_sessions "
            "FROM forward_prediction p "
            "WHERE p.provenance='production' AND p.outcome_due_date <= ? "
            "AND NOT EXISTS (SELECT 1 FROM forward_outcome o "
            "                WHERE o.prediction_id=p.prediction_id)",
            (collection_date.isoformat(),),
        ).fetchall()

    waiting: dict[str, dict] = {}
    oldest: str | None = None
    for row in pending:
        result = _evaluate(
            research_db,
            security_id=str(row["security_id"]),
            as_of=str(row["as_of"])[:10],
            horizon_sessions=int(row["horizon_sessions"]),
            collection_date=collection_date,
        )
        if result["status"] is not None or result["pending"] != AWAITING_ENTRY_BAR:
            continue
        as_of = str(row["as_of"])[:10]
        oldest = as_of if oldest is None or as_of < oldest else oldest
        entry = waiting.setdefault(
            str(row["security_id"]), {"predictions": 0, "oldest_as_of": as_of}
        )
        entry["predictions"] += 1
        entry["oldest_as_of"] = min(entry["oldest_as_of"], as_of)

    if not waiting:
        return {
            "total": 0,
            "distinct_securities": 0,
            "oldest_as_of": None,
            "age_days": None,
            "age_sessions": None,
            "recoverable": {"securities": 0, "predictions": 0},
            "unrecoverable": [],
            "note": "pending by design; never terminalised on a timeout",
        }

    retired = {str(t).upper() for t in retired_tickers()}
    with research_db.connect(read_only=True) as conn:
        tickers = {
            str(r["security_id"]): (
                (str(r["canonical_ticker"]).upper() if r["canonical_ticker"] else None),
                (str(r["delisted_at"])[:10] if r["delisted_at"] else None),
            )
            for r in conn.execute(
                "SELECT security_id, canonical_ticker, delisted_at FROM security "
                "WHERE security_id IN ({})".format(",".join("?" * len(waiting))),
                tuple(waiting),
            ).fetchall()
        }

    recoverable = {"securities": 0, "predictions": 0}
    unrecoverable: list[dict] = []
    for security_id, info in sorted(waiting.items()):
        ticker, delisted_at = tickers.get(security_id, (None, None))
        if security_id not in tickers:
            reason = "unknown_security"
        elif delisted_at is not None:
            reason = "delisted"
        elif ticker is not None and ticker in retired:
            reason = "retired"
        else:
            recoverable["securities"] += 1
            recoverable["predictions"] += info["predictions"]
            continue
        unrecoverable.append(
            {
                "security_id": security_id,
                "ticker": ticker,
                "reason": reason,
                "predictions": info["predictions"],
                "oldest_as_of": info["oldest_as_of"],
            }
        )

    oldest_entry = None
    if oldest is not None:
        from tradehub_research.validation.horizons import entry_session_for

        oldest_entry = entry_session_for(oldest)
    return {
        "total": sum(info["predictions"] for info in waiting.values()),
        "distinct_securities": len(waiting),
        "oldest_as_of": oldest,
        "age_days": (collection_date - date.fromisoformat(oldest)).days if oldest else None,
        "age_sessions": (
            count_sessions(date.fromisoformat(oldest_entry), collection_date)
            if oldest_entry
            else None
        ),
        "recoverable": recoverable,
        "unrecoverable": unrecoverable,
        # Deliberately NOT terminalized: see the docstring.
        "note": "pending by design; never terminalised on a timeout",
    }


def forward_health(
    *,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    collection_date=None,
    reporting_day: date | None = None,
) -> dict:
    """Production forward-ledger health (provenance='production' only).

    ``matured_today`` counts production outcomes APPENDED on ``reporting_day``
    (default: today, local) using the durable ``appended_at`` timestamp -- never
    inferred from a prediction's due date. ``matured_by_horizon`` stays the
    cumulative per-horizon total, which is its documented meaning.

    ``awaiting_entry`` exposes predictions that are due but have no usable entry
    bar, with age and recoverability; they are pending on purpose and are never
    converted to a terminal status by the passage of time alone.
    """
    paths = paths or research_paths()
    collection = collection_date or last_completed_us_session()
    due = collection.isoformat()
    day = reporting_day or date.today()
    day_start, day_end = _local_day_utc_bounds(day)
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
        matured_by_horizon = conn.execute(
            "SELECT p.horizon_sessions, COUNT(*) FROM forward_outcome o "
            "JOIN forward_prediction p ON p.prediction_id=o.prediction_id "
            "WHERE p.provenance='production' "
            "GROUP BY p.horizon_sessions ORDER BY p.horizon_sessions"
        ).fetchall()
        last_screen = conn.execute(
            "SELECT MAX(as_of) FROM forward_prediction WHERE provenance='production'"
        ).fetchone()[0]
        matured_today = conn.execute(
            "SELECT COUNT(*) FROM forward_outcome o "
            "JOIN forward_prediction p ON p.prediction_id=o.prediction_id "
            "WHERE p.provenance='production' AND o.appended_at >= ? AND o.appended_at < ?",
            (day_start, day_end),
        ).fetchone()[0]
    return {
        "production_predictions": total,
        "by_horizon": {str(r[0]): r[1] for r in by_horizon},
        "predictions_due": due_count,
        "matured": {str(r[0]): r[1] for r in matured},
        "matured_by_horizon": {str(r[0]): r[1] for r in matured_by_horizon},
        # Outcomes APPENDED on the reporting day (durable appended_at) and the day
        # they were measured for, so a reader can never mistake this for a
        # lifetime total. `matured_by_horizon` remains the cumulative figure.
        "matured_today": matured_today,
        "matured_today_day": day.isoformat(),
        # Due but not evaluable, with age + recoverability. Pending on purpose:
        # never terminalised by elapsed time (forward_outcome is append-only).
        "awaiting_entry": _awaiting_entry_bar(
            experiment_db, ResearchDB(paths.research_db), collection
        ),
        "last_production_screen": last_screen,
        "generated_at": utc_now(),
    }


def refresh_health(
    *,
    settings: ResearchSettings,
    paths: ResearchPaths | None = None,
    as_of=None,
) -> dict:
    """Market-data freshness vs the refresh contract (7-day rolling window).

    A name is STALE only when its last bar is older than the daily refresh's
    rolling staleness window (REFRESH_STALENESS_DAYS) -- a 1-session lag is
    within the designed rolling coverage and is NOT flagged. Stale names are
    listed honestly (never backfilled).
    """
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    from tradehub_research.ops.daily_refresh import REFRESH_STALENESS_DAYS, retired_tickers

    as_of = (as_of or last_completed_us_session()).isoformat()
    freshness_cutoff = (
        date.fromisoformat(as_of) - timedelta(days=REFRESH_STALENESS_DAYS)
    ).isoformat()
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
    retired = retired_tickers()
    stale = [
        {"ticker": r["canonical_ticker"], "last_bar": r["last_bar"]}
        for r in with_data
        if r["last_bar"] < freshness_cutoff
        and str(r["canonical_ticker"] or "").upper() not in retired
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
    paths = research_paths()
    exp = ExperimentDB(paths.experiment_db)
    out = {
        "forward": forward_health(experiment_db=exp, paths=paths),
        "refresh": refresh_health(settings=settings, paths=paths),
    }
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

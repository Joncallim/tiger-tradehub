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

#: Pending-reason constants (imported lazily to keep the module import light).
_PENDING = {
    "entry": "AWAITING_ENTRY_BAR",
    "exit": "AWAITING_EXIT_BAR",
    "horizon": "AWAITING_HORIZON",
}


def _block(pending: dict[str, dict], reason: str) -> dict:
    """One pending bucket, or a zeroed block when nothing is in that state."""
    return pending.get(reason) or _empty_pending_block()


def _empty_pending_block() -> dict:
    return {
        "total": 0,
        "distinct_securities": 0,
        "oldest_as_of": None,
        "age_days": None,
        "age_sessions": None,
        "oldest_required_exit_session": None,
        "age_sessions_past_exit": None,
        "recoverable": {"securities": 0, "predictions": 0},
        "unrecoverable": [],
        "note": "pending by design; never terminalised on a timeout",
    }


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


def _pending_evidence(
    experiment_db: ExperimentDB, research_db: ResearchDB, collection_date: date
) -> dict[str, dict]:
    """Due-but-unmaterialised predictions, bucketed by PENDING reason.

    One pass over the due set evaluates each prediction with the maturation's own
    classifier, so health and the job can never disagree.

    Pending is deliberate and retryable: ``forward_outcome`` is append-only with
    ``UNIQUE(prediction_id)``, so a permanent label written for a temporary
    evidence gap could never become the genuine OBSERVED outcome. Nothing here is
    converted to a terminal status by elapsed time.

    Buckets:
      * ``AWAITING_ENTRY_BAR``  -- the expected entry session has no usable bar;
      * ``AWAITING_HORIZON``    -- market time has not elapsed yet (normal);
      * ``AWAITING_EXIT_BAR``   -- horizon elapsed, required exit evidence missing
                                   (a data-quality gap, not a verdict).

    Each security is additionally classified ``recoverable`` or, on positive
    evidence, ``delisted`` / ``retired`` / ``unknown_security`` -- operational
    classification only; the maturation still decides terminal labels.
    """
    from tradehub_research.ops.daily_refresh import retired_tickers
    from tradehub_research.ops.outcome_maturation import PENDING_REASONS, _evaluate
    from tradehub_research.validation.horizons import entry_session_for, required_exit_session

    with experiment_db.connect(read_only=True) as conn:
        due_rows = conn.execute(
            "SELECT p.prediction_id, p.security_id, p.as_of, p.horizon_sessions "
            "FROM forward_prediction p "
            "WHERE p.provenance='production' AND p.outcome_due_date <= ? "
            "AND NOT EXISTS (SELECT 1 FROM forward_outcome o "
            "                WHERE o.prediction_id=p.prediction_id)",
            (collection_date.isoformat(),),
        ).fetchall()

    buckets: dict[str, dict[str, dict]] = {reason: {} for reason in PENDING_REASONS}
    for row in due_rows:
        as_of = str(row["as_of"])[:10]
        horizon = int(row["horizon_sessions"])
        result = _evaluate(
            research_db,
            security_id=str(row["security_id"]),
            as_of=as_of,
            horizon_sessions=horizon,
            collection_date=collection_date,
        )
        reason = result["pending"]
        if result["status"] is not None or reason not in buckets:
            continue
        exit_session = required_exit_session(entry_session_for(as_of), horizon)
        security_id = str(row["security_id"])
        info = buckets[reason].setdefault(
            security_id,
            {"predictions": 0, "oldest_as_of": as_of, "oldest_exit_session": exit_session},
        )
        info["predictions"] += 1
        if as_of < info["oldest_as_of"]:
            info["oldest_as_of"] = as_of
            info["oldest_exit_session"] = exit_session

    retired = {str(t).upper() for t in retired_tickers()}
    all_ids = {sid for bucket in buckets.values() for sid in bucket}
    tickers: dict[str, tuple[str | None, str | None]] = {}
    if all_ids:
        with research_db.connect(read_only=True) as conn:
            tickers = {
                str(r["security_id"]): (
                    (str(r["canonical_ticker"]).upper() if r["canonical_ticker"] else None),
                    (str(r["delisted_at"])[:10] if r["delisted_at"] else None),
                )
                for r in conn.execute(
                    "SELECT security_id, canonical_ticker, delisted_at FROM security "
                    "WHERE security_id IN ({})".format(",".join("?" * len(all_ids))),
                    tuple(all_ids),
                ).fetchall()
            }

    blocks: dict[str, dict] = {}
    for reason, bucket in buckets.items():
        recoverable = {"securities": 0, "predictions": 0}
        unrecoverable: list[dict] = []
        for security_id, info in sorted(bucket.items()):
            ticker, delisted_at = tickers.get(security_id, (None, None))
            if security_id not in tickers:
                classification = "unknown_security"
            elif delisted_at is not None:
                classification = "delisted"
            elif ticker is not None and ticker in retired:
                classification = "retired"
            else:
                recoverable["securities"] += 1
                recoverable["predictions"] += info["predictions"]
                continue
            unrecoverable.append(
                {
                    "security_id": security_id,
                    "ticker": ticker,
                    "reason": classification,
                    "predictions": info["predictions"],
                    "oldest_as_of": info["oldest_as_of"],
                }
            )
        oldest = min((i["oldest_as_of"] for i in bucket.values()), default=None)
        oldest_exit = None
        if bucket:
            oldest_exit = min(
                (i["oldest_exit_session"] for i in bucket.values() if i["oldest_as_of"] == oldest),
                default=None,
            )
        blocks[reason] = {
            "total": sum(i["predictions"] for i in bucket.values()),
            "distinct_securities": len(bucket),
            "oldest_as_of": oldest,
            "age_days": (collection_date - date.fromisoformat(oldest)).days if oldest else None,
            "age_sessions": (
                count_sessions(date.fromisoformat(entry_session_for(oldest)), collection_date)
                if oldest
                else None
            ),
            # Only meaningful for the exit-data gap: how long the required exit
            # evidence has been missing in market time.
            "oldest_required_exit_session": oldest_exit,
            "age_sessions_past_exit": (
                count_sessions(date.fromisoformat(oldest_exit), collection_date)
                if oldest_exit and collection_date > date.fromisoformat(oldest_exit)
                else None
            ),
            "recoverable": recoverable,
            "unrecoverable": unrecoverable,
            "note": "pending by design; never terminalised on a timeout",
        }
    return blocks


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

    ``awaiting_entry`` / ``awaiting_exit`` / ``awaiting_horizon`` expose
    predictions that are due but not yet evaluable -- missing entry bar, missing
    required exit evidence once the horizon has elapsed, or the horizon simply not
    elapsed yet -- each with age and recoverability. All are pending on purpose and
    are never converted to a terminal status by the passage of time alone.
    """
    paths = paths or research_paths()
    collection = collection_date or last_completed_us_session()
    due = collection.isoformat()
    day = reporting_day or date.today()
    day_start, day_end = _local_day_utc_bounds(day)
    pending = _pending_evidence(experiment_db, ResearchDB(paths.research_db), collection)
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
        # Due but not evaluable, bucketed by pending reason, with age and
        # recoverability. Pending on purpose: never terminalised by elapsed time,
        # because forward_outcome is append-only and a gap could otherwise never
        # heal into the genuine OBSERVED outcome.
        "awaiting_entry": _block(pending, _PENDING["entry"]),
        "awaiting_exit": _block(pending, _PENDING["exit"]),
        "awaiting_horizon": _block(pending, _PENDING["horizon"]),
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

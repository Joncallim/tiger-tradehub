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

ENTRY SESSION (fixed 2026-09-22, before the first production cohort matured)

The entry session is the first VALID MARKET SESSION strictly after the
prediction's ``as_of``, taken from the market calendar -- not "whichever bar comes
first". A price bar dated on a weekend or a market holiday is not a session and
is dropped outright: it may never establish an entry, an exit, a session count or
a due/maturity date. Live example: ``METRY`` (METRO INC./ADR) carries
calendar-daily Tiingo bars (194 of its 621 bars fall on weekends/holidays), which
previously made a Saturday bar the "entry session". If the expected entry
session's bar is missing, the prediction is honestly not yet evaluable
(``AWAITING_ENTRY_BAR``) and is retried -- the entry is NEVER shifted to a later
session, because that would silently price a different prediction.

HONEST STATES

The outcome table is append-only with ``UNIQUE(prediction_id)``: ONE outcome per
prediction, forever. A wrong terminal classification therefore cannot be undone,
so on the LIVE ledger a permanent non-OBSERVED label is emitted ONLY on positive
evidence that the outcome can never be recovered:

  * the expected entry session has no usable bar -> **AWAITING_ENTRY_BAR**
    (pending, no row, retried; the entry is never shifted to a later session);
  * the horizon has not elapsed in market time -> **AWAITING_HORIZON**
    (pending, no row);
  * the horizon HAS elapsed but the required canonical exit evidence is missing,
    or a realized close is unusable -> **AWAITING_EXIT_BAR** (pending, no row,
    retried). This is a DATA-QUALITY gap, not a verdict: an ingestion/backfill
    repair or a superseded bar can still produce the genuine OBSERVED outcome,
    and an already-appended row could never become it;
  * the security is verifiably delisted at or before the session the horizon
    needs -> ``DELISTING_OUTCOME_UNKNOWN`` (terminal; the research contract's
    explicit class -- never dropped, never imputed zero).

There is deliberately NO elapsed-time timeout that converts a data-quality gap
into a permanent label.

FROZEN SNAPSHOT vs LIVE LEDGER (explicit distinction)

``validation/outcome_builder.py`` labels a FROZEN dataset snapshot, where the
observation boundary is final: there, fewer realized sessions than the horizon
is terminally ``CENSORED_INSUFFICIENT_HORIZON``, and that contract is unchanged.
That label is never emitted by this live path.

``mature_due_outcomes`` never passes a status outside the schema's enum (the
pre-fix path could return ``ENTRY_UNAVAILABLE``, which the CHECK constraint
rejects -- an insert that would abort the whole run).
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from typing import Any

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import (
    EvaluationClock,
    ResearchPaths,
    evaluation_clock,
    research_paths,
)
from tradehub_research.portfolio.prices import _session_key
from tradehub_research.validation import outcome_prices
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import append_outcome
from tradehub_research.validation.horizons import (
    HORIZON_SESSIONS,
    entry_session_for,
    required_exit_session,
)

#: Statuses the ``forward_outcome`` CHECK constraint accepts (schema surface).
#: ``CENSORED_INSUFFICIENT_HORIZON`` legitimately belongs to the FROZEN-snapshot
#: research builder (its dataset boundary is final). This live forward path does
#: NOT emit it: for the live ledger a missing bar is a repairable data-quality
#: gap, and ``forward_outcome`` is append-only + UNIQUE(prediction_id), so a
#: permanent label written for a gap could never become the genuine OBSERVED
#: outcome. The live path emits only OBSERVED or DELISTING_OUTCOME_UNKNOWN.
TERMINAL_STATUSES = ("OBSERVED", "DELISTING_OUTCOME_UNKNOWN", "CENSORED_INSUFFICIENT_HORIZON")
EMITTABLE_STATUSES = ("OBSERVED", "DELISTING_OUTCOME_UNKNOWN")

#: Pending (no row appended) reasons. Pending is retryable by design; a
#: temporary evidence gap can heal into OBSERVED on a later run.
AWAITING_ENTRY_BAR = "AWAITING_ENTRY_BAR"
AWAITING_HORIZON = "AWAITING_HORIZON"  # market time has not elapsed yet (normal)
AWAITING_EXIT_BAR = "AWAITING_EXIT_BAR"  # horizon elapsed, required evidence missing
PENDING_REASONS = (AWAITING_ENTRY_BAR, AWAITING_HORIZON, AWAITING_EXIT_BAR)


def _security_state(db: Any, security_id: str) -> tuple[bool, str | None]:
    """``(exists, delisted_at)`` for a security, from an open connection.

    A security that is absent from the table, or carries a ``delisted_at``, is
    reported operationally (see ``ops/health.py``). Only a verified delisting
    at/before the session the horizon needs is a TERMINAL label.
    """
    row = db.execute(
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
    clock: EvaluationClock,
    records_cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict:
    """Classify one due prediction against its realized sessions and evidence.

    Returns ``{"status": <enum or None>, "pending": <reason or None>,
    "raw_return"|"total_return": float | None, "entry_session_date": str | None,
    "exit_session_date": str | None, "detail": str}``.

    Evidence authority: every bar and corporate action is read through
    ``validation.outcome_prices`` -> ``portfolio.prices._visible_records``, i.e.
    the same supersession / withdrawal / publication-visibility / approved-PAT
    semantics the rest of the repository uses, bounded by the COLLECTION DATE so
    a correction published later is never consumed early. The entry price
    convention, the exact exit session and the raw/total return definitions are
    the shared ones -- identical to the frozen research builder.
    """
    # Two clocks, kept apart (see ops.common.EvaluationClock):
    #   * visibility  = the actual evaluation timestamp -- evidence published after
    #     it is never consumed early (the real Tiingo PAT is 20:15 ET -> UTC, i.e.
    #     the next UTC day for a US session);
    #   * maturity    = the market-session cutoff -- a horizon has elapsed only
    #     when the calendar says the required exit session is complete.
    bound = clock.visibility_bound
    expected_entry = entry_session_for(as_of)
    expected_exit = required_exit_session(expected_entry, horizon_sessions)

    with research_db.connect(read_only=True) as db:
        # ONE evidence resolution per evaluation (chain resolution loads the
        # security's whole history -- never do it three times per row). A caller
        # working over one collection date may pass a cache keyed by security_id:
        # the visibility bound is the collection date, so the resolution is shared
        # by every cohort and horizon evaluated in that run.
        if records_cache is None:
            records = outcome_prices.visible_records(db, security_id, bound)
        else:
            records = records_cache.get(security_id)
            if records is None:
                records = outcome_prices.visible_records(db, security_id, bound)
                records_cache[security_id] = records
        _exists, delisted_at = _security_state(db, security_id)

        # Terminal on positive evidence: verified delisting at/before the session
        # the horizon needs means the outcome can never be observed.
        if delisted_at is not None and delisted_at <= expected_exit:
            entry_session_known = None
            _bar, session, _price, _conv = outcome_prices.entry_for(
                db, security_id, as_of, visibility_bound=bound, records=records
            )
            if session is not None:
                entry_session_known = session
            return {
                "status": "DELISTING_OUTCOME_UNKNOWN",
                "pending": None,
                "entry_session_date": entry_session_known,
                "detail": (
                    f"delisted {delisted_at} at/before the required exit session {expected_exit}"
                ),
            }

        # Entry: the calendar's expected entry session, from the canonical
        # visible/terminal evidence layer.
        entry_bar, entry_session, entry_price, entry_convention = outcome_prices.entry_for(
            db, security_id, as_of, visibility_bound=bound, records=records
        )
        if entry_bar is None or entry_session is None:
            return {
                "status": None,
                "pending": AWAITING_ENTRY_BAR,
                "detail": (
                    f"expected entry session {expected_entry} has no visible terminal bar "
                    "(never shifted to a later session)"
                ),
            }
        if entry_price is None:
            # Unusable ENTRY data is an entry gap, not an exit gap.
            return {
                "status": None,
                "pending": AWAITING_ENTRY_BAR,
                "entry_session_date": entry_session,
                "detail": (
                    f"entry session {entry_session} carries no usable (positive) "
                    f"{entry_convention} price"
                ),
            }

        exit_session = required_exit_session(entry_session, horizon_sessions)
        if delisted_at is not None and delisted_at <= exit_session:
            return {
                "status": "DELISTING_OUTCOME_UNKNOWN",
                "pending": None,
                "entry_session_date": entry_session,
                "detail": f"delisted {delisted_at} at/before the exit session {exit_session}",
            }

        if clock.session_cutoff < date.fromisoformat(exit_session):
            # Market time has not elapsed yet: normal waiting, not a gap.
            return {
                "status": None,
                "pending": AWAITING_HORIZON,
                "entry_session_date": entry_session,
                "detail": f"exit session {exit_session} has not arrived",
            }

        # Exit: EXACTLY the required session, from the same visible evidence set.
        bars = outcome_prices.visible_session_bars(
            db,
            security_id,
            after_session=entry_session,
            up_to=bound,
            visibility_bound=bound,
            records=records,
        )
        exit_bar = next(
            (bar for bar in bars if _session_key(bar) == exit_session),
            None,
        )
        if exit_bar is None:
            # Missing, or that session is UNKNOWN because its bars conflict. A
            # DATA-QUALITY gap, not a verdict: forward_outcome is append-only with
            # UNIQUE(prediction_id), so a permanent non-OBSERVED row written now
            # could never become the genuine OBSERVED outcome. Stay pending and
            # retry. (The frozen-snapshot builder is different: there the dataset
            # boundary is final, so an insufficient horizon is terminally
            # CENSORED -- see validation/outcome_builder.py.)
            return {
                "status": None,
                "pending": AWAITING_EXIT_BAR,
                "entry_session_date": entry_session,
                "detail": (
                    f"{len(bars)}/{horizon_sessions} realized sessions; no visible canonical "
                    f"exit bar for {exit_session}"
                ),
            }

        exit_close = outcome_prices.bar_close(exit_bar)
        if exit_close is None or exit_close <= 0:
            return {
                "status": None,
                "pending": AWAITING_EXIT_BAR,
                "entry_session_date": entry_session,
                "detail": (
                    f"exit bar for {exit_session} carries no usable (positive) close "
                    "(recoverable: the record may be corrected)"
                ),
            }

        raw_return, total_return = outcome_prices.outcome_returns(
            db,
            security_id,
            entry_session=entry_session,
            entry_price=entry_price,
            exit_session=exit_session,
            exit_close=exit_close,
            visibility_bound=bound,
            records=records,
        )
        return {
            "status": "OBSERVED",
            "pending": None,
            "raw_return": float(raw_return) if raw_return is not None else None,
            "total_return": float(total_return) if total_return is not None else None,
            "entry_session_date": entry_session,
            "exit_session_date": exit_session,
            "detail": (
                f"{horizon_sessions} sessions {entry_session} -> {exit_session} "
                f"({entry_convention})"
            ),
        }


def mature_due_outcomes(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    now: datetime | None = None,
) -> dict:
    """Append outcomes for production predictions that are due for evaluation.

    ``now`` (default: the current UTC instant) is the injectable EVALUATION CLOCK:
    ``outcome_due_date`` is only the coarse advisory scheduling gate, the market
    session cutoff comes from the exchange calendar, and evidence visibility is
    bounded by ``now`` itself (see ``ops.common.EvaluationClock``). Whether a
    horizon has matured is verified separately, from realized sessions.
    """
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    clock = evaluation_clock(now)

    with experiment_db.connect(read_only=True) as conn:
        pending = conn.execute(
            "SELECT p.prediction_id, p.security_id, p.as_of, p.horizon_sessions "
            "FROM forward_prediction p "
            "WHERE p.provenance='production' AND p.outcome_due_date <= ? "
            "AND NOT EXISTS (SELECT 1 FROM forward_outcome o "
            "WHERE o.prediction_id=p.prediction_id)",
            (clock.evaluation_date.isoformat(),),
        ).fetchall()

    counts = {status: 0 for status in TERMINAL_STATUSES}
    awaiting = {reason: 0 for reason in PENDING_REASONS}
    materialized = 0
    # The classification depends only on (security, as_of, horizon): the variants
    # of one prediction share it, so evaluate each unit ONCE and apply the result
    # to every prediction in it (evidence resolution is the expensive part).
    evaluated: dict[tuple[str, str, int], dict] = {}
    records_cache: dict[str, list[dict[str, Any]]] = {}
    for row in pending:
        horizon = int(row["horizon_sessions"])
        if horizon not in HORIZON_SESSIONS:
            awaiting["UNKNOWN_HORIZON"] = awaiting.get("UNKNOWN_HORIZON", 0) + 1
            continue
        key = (str(row["security_id"]), str(row["as_of"])[:10], horizon)
        result = evaluated.get(key)
        if result is None:
            result = _evaluate(
                research_db,
                security_id=key[0],
                as_of=key[1],
                horizon_sessions=horizon,
                clock=clock,
                records_cache=records_cache,
            )
            evaluated[key] = result
        if result["status"] is None:
            awaiting[result["pending"]] = awaiting.get(result["pending"], 0) + 1
            continue
        append_outcome(
            experiment_db,
            prediction_id=str(row["prediction_id"]),
            outcome_status=result["status"],
            raw_return=result.get("raw_return"),
            total_return=result.get("total_return"),
            entry_session_date=result.get("entry_session_date"),
            exit_session_date=result.get("exit_session_date"),
        )
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        materialized += 1

    return {
        "status": "OK",
        "evaluation_now": clock.visibility_bound,
        "session_cutoff": clock.session_cutoff.isoformat(),
        "due": len(pending),
        "materialized": materialized,
        # Rows appended by THIS run, keyed by the status written.
        "matured": {status: n for status, n in counts.items() if n},
        # Due but honestly not evaluable yet (retryable): no row was appended.
        "awaiting": {reason: n for reason, n in awaiting.items() if n},
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

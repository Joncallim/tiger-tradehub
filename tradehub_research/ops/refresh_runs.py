"""Durable record of what each daily refresh run actually did.

The refresh is **bounded by design**: it spends a rotation budget and
deliberately defers the remainder to the next run. Without a durable record of
that decision, the diagnosis cannot tell a successful bounded run from an
interrupted one, so a deliberately deferred symbol is reported as an
``INTERRUPTED_INGESTION_BATCH`` and remediation spends provider quota
re-fetching work the rotation had already scheduled. The run summary alone is
not evidence -- it is stdout, and it dies with the process.

One row per expected session, plus one row per candidate the run considered:

* ``COMPLETED``      -- the run served its budget and closed normally. Only a
  completed run may be read as a deliberate deferral.
* ``QUOTA_EXHAUSTED``-- the provider reserve stopped it early. Not deliberate.
* ``RUNNING``        -- still open, or the worker died before finishing. The
  deferral list is then absent and every stale symbol falls back to the
  conservative "interrupted" path.

That asymmetry is the point: a missing or unfinished run must never *suppress*
remediation, and a completed one must never be dressed up as an interruption.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: File name under the research dir (same neighbourhood as
#: ``freshness_remediation.sqlite``; both are operational state, not evidence).
REFRESH_RUNS_DB = "refresh_runs.sqlite"

RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"

REFRESHED = "REFRESHED"
FAILED = "FAILED"
#: A candidate the run did not reach because the rotation budget was spent.
DEFERRED_BUDGET = "BUDGET_EXHAUSTED"
#: A candidate withheld by the fairness slice because it keeps failing.
DEFERRED_COOLING = "COOLING_SLICE"
#: A candidate the rolling-month symbol ceiling would not admit.
DEFERRED_CAPACITY = "CAPACITY_DEFERRED"

#: The reasons a symbol can be missing from a *completed* run's work.
DEFERRED = frozenset({DEFERRED_BUDGET, DEFERRED_COOLING, DEFERRED_CAPACITY})

_REASONS_TEXT = {
    DEFERRED_BUDGET: "rotation budget spent before it was reached",
    DEFERRED_COOLING: "withheld by the fairness slice (recent fetch failures)",
    DEFERRED_CAPACITY: "rolling-month symbol ceiling would not admit it",
}


def deferral_reason_text(reason: str) -> str:
    """Operator-readable reason for a deliberate deferral."""
    return _REASONS_TEXT.get(reason, reason.replace("_", " ").lower())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RefreshRunStore:
    """Per-session refresh outcome, durable across processes.

    Mirrors :class:`tradehub_research.ops.data_freshness.CheckpointStore`: a
    small WAL SQLite file under the research dir, written by the refresh and
    read by the diagnosis. No schema migration of the large databases.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS refresh_run(
                    run_key TEXT PRIMARY KEY,
                    expected_session TEXT NOT NULL,
                    invocation_id TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL DEFAULT 'RUNNING',
                    universe INTEGER,
                    window_sessions INTEGER,
                    rotation_budget INTEGER,
                    candidates INTEGER,
                    refreshed INTEGER,
                    deferred INTEGER
                );
                CREATE TABLE IF NOT EXISTS refresh_symbol(
                    run_key TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (run_key, ticker)
                );
                CREATE INDEX IF NOT EXISTS refresh_symbol_disposition
                    ON refresh_symbol(run_key, disposition);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # --- writer ----------------------------------------------------------
    def open_run(
        self,
        run_key: str,
        expected_session: str,
        *,
        universe: int | None = None,
        window_sessions: int | None = None,
        rotation_budget: int | None = None,
        candidates: int | None = None,
    ) -> str:
        """Claim the session for this invocation; returns its token.

        A re-run of the same session resets it to ``RUNNING``: the deferral list
        from an earlier partial attempt must not survive as if it were the
        finished run's decision.

        The returned token must be passed to :meth:`update_metadata` and
        :meth:`finish`. Two same-session invocations can overlap, and without the
        token the earlier one's ``finish()`` could close the row the later one had
        just reset -- marking an interrupted session ``COMPLETED`` and letting its
        obsolete deferrals suppress remediation. Every later write is therefore
        conditioned on the token this call mints.
        """
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO refresh_run(run_key, expected_session, invocation_id, started_at, "
                "status, universe, window_sessions, rotation_budget, candidates) "
                "VALUES (?,?,?,?,'RUNNING',?,?,?,?) "
                "ON CONFLICT(run_key) DO UPDATE SET invocation_id=excluded.invocation_id, "
                "started_at=excluded.started_at, "
                "finished_at=NULL, status='RUNNING', universe=excluded.universe, "
                "window_sessions=excluded.window_sessions, "
                "rotation_budget=excluded.rotation_budget, candidates=excluded.candidates",
                (
                    run_key,
                    expected_session,
                    token,
                    _utc_now(),
                    universe,
                    window_sessions,
                    rotation_budget,
                    candidates,
                ),
            )
        return token

    def update_metadata(
        self,
        run_key: str,
        *,
        token: str,
        window_sessions: int | None = None,
        rotation_budget: int | None = None,
        candidates: int | None = None,
    ) -> bool:
        """Fill in facts discovered after the run was opened.

        Kept separate from :meth:`open_run` because the run must be marked
        ``RUNNING`` *before* the first fallible request, while the window, budget
        and demand are only known once the universe has been read. Refuses and
        returns ``False`` when this invocation no longer owns the row.
        """
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE refresh_run SET window_sessions=COALESCE(?, window_sessions), "
                "rotation_budget=COALESCE(?, rotation_budget), "
                "candidates=COALESCE(?, candidates) WHERE run_key=? AND invocation_id=?",
                (window_sessions, rotation_budget, candidates, run_key, token),
            )
            return cursor.rowcount > 0

    def finish(
        self,
        run_key: str,
        status: str,
        outcomes: dict[str, str],
        *,
        token: str,
        candidates: int | None = None,
        refreshed: int | None = None,
        deferred: int | None = None,
    ) -> bool:
        """Close the run and record what happened to every candidate.

        ``outcomes`` covers every symbol the run touched -- the active phase
        included -- because that is what the diagnosis reads per symbol. The
        ``candidates``/``refreshed``/``deferred`` totals are the **rotation**
        numbers the report quotes against the rotation budget, so the caller
        passes them: deriving them from ``outcomes`` would count an active-set
        success as a rotation candidate served and let the report claim more work
        than the budget permits, on a demand larger than the rotation ever had.

        Omitted values fall back to ``outcomes`` (correct when the run had no
        active phase). Written atomically with the status: a reader must never see
        a closed run with a half-written work list.

        Returns ``False`` without writing anything when ``token`` is no longer the
        row's invocation -- a newer same-session run has taken ownership, and this
        run's outcome must not become the last word for the session.
        """
        now = _utc_now()
        refreshed = (
            refreshed
            if refreshed is not None
            else sum(1 for d in outcomes.values() if d == REFRESHED)
        )
        deferred = (
            deferred if deferred is not None else sum(1 for d in outcomes.values() if d in DEFERRED)
        )
        candidates = candidates if candidates is not None else len(outcomes)
        db = self._connect()
        try:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT invocation_id FROM refresh_run WHERE run_key=?", (run_key,)
            ).fetchone()
            if row is None or row["invocation_id"] != token:
                db.execute("ROLLBACK")
                return False
            db.execute("DELETE FROM refresh_symbol WHERE run_key=?", (run_key,))
            db.executemany(
                "INSERT INTO refresh_symbol(run_key, ticker, disposition, recorded_at) "
                "VALUES (?,?,?,?)",
                [(run_key, ticker, disposition, now) for ticker, disposition in outcomes.items()],
            )
            db.execute(
                "UPDATE refresh_run SET finished_at=?, status=?, refreshed=?, "
                "deferred=?, candidates=? WHERE run_key=?",
                (now, status, refreshed, deferred, candidates, run_key),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        return True

    # --- reader ----------------------------------------------------------
    def run(self, run_key: str) -> dict[str, Any] | None:
        """The session's run record, whatever its state (None if never started)."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM refresh_run WHERE run_key=?", (run_key,)).fetchone()
        return dict(row) if row else None

    def outcomes(self, run_key: str) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT ticker, disposition FROM refresh_symbol WHERE run_key=?", (run_key,)
            ).fetchall()
        return {str(r["ticker"]): str(r["disposition"]) for r in rows}

    def completed_deferrals(self, run_key: str) -> dict[str, str]:
        """Deliberately deferred tickers of a **COMPLETED** run: {ticker: reason}.

        Empty when the run never started, never finished, or stopped on quota --
        i.e. whenever "deferred on purpose" would be an unfounded claim.
        """
        record = self.run(run_key)
        if not record or record.get("status") != COMPLETED:
            return {}
        return {
            ticker: disposition
            for ticker, disposition in self.outcomes(run_key).items()
            if disposition in DEFERRED
        }

    def completed_run(self, run_key: str) -> dict[str, Any] | None:
        """The run record only when it COMPLETED (the deliberate-deferral basis)."""
        record = self.run(run_key)
        return record if record and record.get("status") == COMPLETED else None


def store_for(paths) -> RefreshRunStore:
    """The canonical store for a research-paths object."""
    return RefreshRunStore(Path(paths.research_dir) / REFRESH_RUNS_DB)


__all__ = [
    "COMPLETED",
    "DEFERRED",
    "DEFERRED_BUDGET",
    "DEFERRED_CAPACITY",
    "DEFERRED_COOLING",
    "FAILED",
    "QUOTA_EXHAUSTED",
    "REFRESHED",
    "REFRESH_RUNS_DB",
    "RUNNING",
    "RefreshRunStore",
    "deferral_reason_text",
    "store_for",
]

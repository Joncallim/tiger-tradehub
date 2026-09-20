"""Data-freshness diagnosis, bounded automatic remediation and verification.

Problem this solves
-------------------
The health watch reported ``323 securities stale (behind 2026-09-18)`` and did
nothing else: no diagnosis, no repair, no verification, no downstream
protection. The 2026-09-19 incident root cause was structural, not a crash:

    eligible universe            443
    rotation budget               40/day   (ROTATION_REQUESTS_PER_RUN)
    freshness contract            >= 443/7 ~ 63/day

The rotation can never hold its own 7-day freshness contract, so a cohort of
403 names that shared a last-bar date of 2026-09-09 crossed the cutoff together
on the 2026-09-18 run; two runs cleared 40 each, leaving exactly 323.

Design
------
* **Diagnosis** classifies every stale security by root cause, grouping the
  failures instead of reporting N unrelated incidents.
* **Remediation** is bounded, targeted (only the affected symbols, never the
  whole universe), idempotent, quota-respecting and checkpoint-backed, so a
  restart resumes instead of restarting.
* **Verification** re-reads persisted state and only then marks a symbol
  resolved: a job exiting 0 is not evidence.
* **Downstream protection** quarantines anything still stale so it cannot feed
  scans, rankings or signals. Prices are never fabricated or forward-filled.
* **Retry budget** is finite: N attempts inside a window, then the incident is
  classified REQUIRES_INTERVENTION. No infinite loops.

Everything is deterministic given an injected clock; the provider/quota objects
are passed in so tests need no network.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.market_calendar import count_sessions, expected_latest_session, sessions_behind

# --- retry budget -----------------------------------------------------------
MAX_ATTEMPTS_PER_RUN = 3  # immediate retries for transient failures
MAX_ATTEMPTS_TOTAL = 6  # across runs, then REQUIRES_INTERVENTION
REMEDIATION_WINDOW_HOURS = 72  # after this, escalate rather than keep trying
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_CAP_SECONDS = 3600.0
BACKOFF_JITTER_FRACTION = 0.25

# --- classification ---------------------------------------------------------
ROTATION_STARVED = "ROTATION_BUDGET_STARVED"
INTERRUPTED_BATCH = "INTERRUPTED_INGESTION_BATCH"
PROVIDER_THROTTLE = "PROVIDER_THROTTLING"
PROVIDER_TRANSIENT = "TRANSIENT_PROVIDER_ERROR"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
AUTH_FAILURE = "AUTH_FAILURE"
DELISTED_EMPTY = "DELISTED_NO_BARS"
INVALID_SYMBOL = "INVALID_SYMBOL"
VALIDATION_FAILURE = "FETCHED_NOT_STORED"
NO_TRADE_ON_SESSION = "NO_TRADE_ON_EXPECTED_SESSION"
CHECKPOINT_LOST = "CHECKPOINT_LOST_RESUME_GAP"
#: The request range included a session whose publication time has not arrived
#: (Tiingo EOD PAT = session+1 00:15Z). A request-range defect at the caller, not
#: a storage failure and not bad upstream data; the next cycle fixes it.
NOT_YET_PUBLISHED = "UPSTREAM_SESSION_NOT_YET_PUBLISHED"
UNRESOLVED = "UNRESOLVED"

#: Causes that are genuine data defects and therefore remediable.
REMEDIABLE = frozenset(
    {
        ROTATION_STARVED,
        INTERRUPTED_BATCH,
        PROVIDER_THROTTLE,
        PROVIDER_TRANSIENT,
        QUOTA_EXHAUSTED,
        AUTH_FAILURE,
        VALIDATION_FAILURE,
        CHECKPOINT_LOST,
        NOT_YET_PUBLISHED,
        UNRESOLVED,
    }
)

#: Causes that are legitimate exceptions: nothing to fetch, nothing to fix.
LEGITIMATE_EXCEPTIONS = frozenset({DELISTED_EMPTY, INVALID_SYMBOL, NO_TRADE_ON_SESSION})

_DEFAULT_CLASS_BY_ERROR = {
    "RATE_LIMITED": PROVIDER_THROTTLE,
    "PROVIDER_ERROR": PROVIDER_TRANSIENT,
    "NETWORK": PROVIDER_TRANSIENT,
    "QUOTA": QUOTA_EXHAUSTED,
    "AUTH": AUTH_FAILURE,
    "UNKNOWN_SYMBOL": INVALID_SYMBOL,
    "DUPLICATE_CIK": INVALID_SYMBOL,
    "PARSE": VALIDATION_FAILURE,
    "NOT_YET_PUBLISHED": NOT_YET_PUBLISHED,
    "EMPTY": DELISTED_EMPTY,
}


# ---------------------------------------------------------------------------
# Checkpoint store
# ---------------------------------------------------------------------------
class CheckpointStore:
    """Durable per-run, per-symbol progress.

    Answers: which symbols are complete / pending / failed / excluded, when the
    run began, when each symbol was last attempted, and why it failed. A
    restarted worker reads this and continues instead of restarting.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS remediation_run(
                    run_key TEXT PRIMARY KEY,
                    expected_session TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    universe INTEGER,
                    initially_stale INTEGER,
                    status TEXT NOT NULL DEFAULT 'RUNNING'
                );
                CREATE TABLE IF NOT EXISTS remediation_symbol(
                    run_key TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    security_id TEXT,
                    last_bar_before TEXT,
                    last_bar_after TEXT,
                    classification TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    verified_at TEXT,
                    PRIMARY KEY (run_key, ticker)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def open_run(self, run_key: str, expected_session: str, universe: int, stale: int) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO remediation_run(run_key, expected_session, started_at, universe, "
                "initially_stale, status) VALUES (?,?,?,?,?,'RUNNING') "
                "ON CONFLICT(run_key) DO UPDATE SET universe=excluded.universe, "
                "initially_stale=excluded.initially_stale",
                (run_key, expected_session, _utc_now(), universe, stale),
            )

    def close_run(self, run_key: str, status: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE remediation_run SET finished_at=?, status=? WHERE run_key=?",
                (_utc_now(), status, run_key),
            )

    def seed_symbol(
        self, run_key: str, ticker: str, security_id: str | None, last_bar: str | None,
        classification: str,
    ) -> None:
        """Idempotent: an existing row keeps its attempts/outcome."""
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO remediation_symbol"
                "(run_key, ticker, security_id, last_bar_before, classification) VALUES (?,?,?,?,?)",
                (run_key, ticker, security_id, last_bar, classification),
            )

    def pending(self, run_key: str, now: datetime | None = None) -> list[sqlite3.Row]:
        """Symbols still owed work whose backoff has elapsed."""
        now = now or datetime.now(timezone.utc)
        stamp = now.isoformat()
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM remediation_symbol WHERE run_key=? AND disposition='PENDING' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY ticker",
                (run_key, stamp),
            ).fetchall()

    def all_symbols(self, run_key: str) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM remediation_symbol WHERE run_key=? ORDER BY ticker", (run_key,)
            ).fetchall()

    def record_attempt(
        self, run_key: str, ticker: str, *, error: str | None, next_attempt_at: str | None,
    ) -> int:
        with self._connect() as db:
            db.execute(
                "UPDATE remediation_symbol SET attempts=attempts+1, last_attempt_at=?, "
                "last_error=?, next_attempt_at=? WHERE run_key=? AND ticker=?",
                (_utc_now(), error, next_attempt_at, run_key, ticker),
            )
            row = db.execute(
                "SELECT attempts FROM remediation_symbol WHERE run_key=? AND ticker=?",
                (run_key, ticker),
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def settle(
        self, run_key: str, ticker: str, *, disposition: str, last_bar_after: str | None,
        classification: str | None = None, verified: bool = True,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE remediation_symbol SET disposition=?, last_bar_after=?, "
                "classification=COALESCE(?, classification), "
                "verified_at=CASE WHEN ? THEN ? ELSE verified_at END "
                "WHERE run_key=? AND ticker=?",
                (
                    disposition,
                    last_bar_after,
                    classification,
                    "1" if verified else "0",
                    _utc_now(),
                    run_key,
                    ticker,
                ),
            )

    def consistent_completed(self, run_key: str) -> bool:
        """A completed checkpoint: no PENDING rows and a terminal run status."""
        with self._connect() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM remediation_symbol WHERE run_key=? AND disposition='PENDING'",
                (run_key,),
            ).fetchone()[0]
            status = db.execute(
                "SELECT status FROM remediation_run WHERE run_key=?", (run_key,)
            ).fetchone()
        return pending == 0 and status is not None and status["status"] in ("COMPLETED", "ESCALATED")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_key_for(expected_session: date) -> str:
    """One remediation run per expected session (idempotent across re-runs)."""
    return f"freshness-{expected_session.isoformat()}"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
@dataclass
class SecurityFreshness:
    ticker: str
    security_id: str | None
    last_bar: str | None
    missing_sessions: int
    classification: str
    provider: str = "tiingo"
    last_attempt_at: str | None = None
    last_attempt_status: str | None = None
    last_attempt_http: int | None = None
    last_attempt_error: str | None = None
    notes: str | None = None

    @property
    def stale(self) -> bool:
        return self.missing_sessions > 0 or self.last_bar is None


@dataclass
class AuditResult:
    expected_session: str
    universe: int
    fresh: int
    stale: list[SecurityFreshness] = field(default_factory=list)
    exceptions: list[SecurityFreshness] = field(default_factory=list)
    lagging_within_window: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)
    generated_at: str = field(default_factory=_utc_now)

    @property
    def stale_count(self) -> int:
        return len(self.stale)

    # --- reconciled fleet accounting -------------------------------------
    # Every count below has exactly one meaning, and the report renders them so
    # that the arithmetic can be checked by eye:
    #
    #   universe_total = eligible + excluded_exceptions
    #   eligible       = fresh_before + stale_before
    #   fresh_after    = fresh_before + repaired_this_run
    #   stale_after    = stale_before - repaired_this_run - newly_classified_exceptions
    #
    # "Fresh" means "has the expected session's data". It never means "repaired".

    @property
    def universe_total(self) -> int:
        """Every security in the canonical universe, before any exclusion."""
        return self.universe

    @property
    def excluded_exceptions(self) -> int:
        """Legitimately unfetchable: delisted, invalid symbol, no-trade."""
        return len(self.exceptions)

    @property
    def eligible(self) -> int:
        """Securities for which data is expected to exist at all."""
        return self.universe - self.excluded_exceptions

    @property
    def fresh_before(self) -> int:
        """Eligible securities already carrying the expected session, or inside
        the rolling-coverage window (refreshed on schedule by design)."""
        return self.fresh + len(self.lagging_within_window)

    @property
    def stale_before(self) -> int:
        """Eligible securities past the freshness contract at detection time."""
        return len(self.stale)

    def invariant_errors(self) -> list[str]:
        """Empty when the audit reconciles. A non-empty list is a defect in the
        accounting, not in the market data, and must be surfaced rather than
        rendered as a plausible-looking report."""
        problems: list[str] = []
        if self.universe_total != self.eligible + self.excluded_exceptions:
            problems.append(
                f"universe_total({self.universe_total}) != eligible({self.eligible}) "
                f"+ excluded_exceptions({self.excluded_exceptions})"
            )
        if self.eligible != self.fresh_before + self.stale_before:
            problems.append(
                f"eligible({self.eligible}) != fresh_before({self.fresh_before}) "
                f"+ stale_before({self.stale_before})"
            )
        if self.fresh + len(self.lagging_within_window) != self.fresh_before:
            problems.append("fresh_before does not equal at_expected + within_window")
        if not all(s.missing_sessions > 0 or s.last_bar is None for s in self.stale):
            problems.append("a security classified stale is not actually behind")
        return problems

    def as_dict(self) -> dict:
        return {
            "expected_session": self.expected_session,
            "universe_total": self.universe_total,
            "eligible": self.eligible,
            "excluded_exceptions": self.excluded_exceptions,
            "fresh_before": self.fresh_before,
            "at_expected_session": self.fresh,
            "within_rolling_window": len(self.lagging_within_window),
            "stale_before": self.stale_before,
            "stale_count": self.stale_count,
            "lagging_within_window": len(self.lagging_within_window),
            "exception_count": len(self.exceptions),
            "groups": {k: len(v) for k, v in sorted(self.groups.items())},
            "invariant_errors": self.invariant_errors(),
            "stale": [asdict(s) for s in self.stale],
            "generated_at": self.generated_at,
        }


def _classify_from_attempt(error: str | None, status: str | None) -> str | None:
    """Classify a recorded ingestion attempt.

    ``backfill_attempt`` is append-only, so a historical row keeps whatever
    prefix the code wrote at the time (METRY's 2026-09-10 row says ``PARSE:``
    even though the cause was an unpublished session). Classification therefore
    has to be derivable from the stored TEXT, not only from a prefix written by
    a newer version of the classifier.
    """
    if not error:
        if status == "SKIPPED_QUOTA":
            return QUOTA_EXHAUSTED
        return None
    lowered = error.lower()
    if "cannot follow ingested_time" in lowered:
        return NOT_YET_PUBLISHED  # timing, not a storage defect
    head = error.split(":", 1)[0].strip()
    return _DEFAULT_CLASS_BY_ERROR.get(head)


def _last_bar(research_db, security_id: str) -> str | None:
    with research_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT MAX(json_extract(structured_fields, '$.session_date')) AS d "
            "FROM evidence_event WHERE security_id=? AND source_id='tiingo_eod'",
            (security_id,),
        ).fetchone()
    return (row["d"] if row and row["d"] else None)


def _last_attempt(experiment_db, ticker: str) -> dict | None:
    """Most recent ingestion attempt for a symbol, from the append-only ledger."""
    try:
        with experiment_db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT status, http_status, error, requested_at FROM backfill_attempt "
                "WHERE upper(symbol_or_cik)=upper(?) ORDER BY requested_at DESC LIMIT 1",
                (ticker,),
            ).fetchone()
    except Exception:  # noqa: BLE001 -- an older ledger must not break the audit
        return None
    return dict(row) if row else None


def audit_universe(
    *,
    settings,
    paths: ResearchPaths | None = None,
    experiment_db,
    as_of: date | None = None,
    now: datetime | None = None,
    rotation_budget: int | None = None,
) -> AuditResult:
    """Classify every universe security against the expected latest session.

    Read-only. Groups root causes instead of emitting per-symbol incidents.
    """
    from tradehub_research.backfill.tiingo_driver import (
        canonical_tickers_by_cik,
        symbol_has_evidence,
    )
    from tradehub_research.db import ResearchDB
    from tradehub_research.ops.daily_refresh import (
        REFRESH_STALENESS_DAYS,
        RETIRE_GAP_DAYS,
        ROTATION_REQUESTS_PER_RUN,
        retired_tickers,
    )

    paths = paths or research_paths()
    expected = as_of or expected_latest_session(now)
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    canonical = canonical_tickers_by_cik(research_db)
    retired = retired_tickers()
    budget = rotation_budget if rotation_budget is not None else ROTATION_REQUESTS_PER_RUN

    # The refresh design states its window in CALENDAR days
    # (REFRESH_STALENESS_DAYS = 7, "the whole cohort rolls over every few
    # days"), but a market-data contract is measured in SESSIONS: weekends and
    # holidays must never count against a symbol. Convert the design's calendar
    # window into the sessions that actually fall inside it, so both sides of
    # the contract speak the same unit.
    window_sessions = max(count_sessions(expected - timedelta(days=REFRESH_STALENESS_DAYS), expected), 1)

    # Structural diagnosis: can the refresh budget hold the contract at all?
    required_daily = -(-len(canonical) // window_sessions)  # ceil
    budget_starved = budget < required_daily

    result = AuditResult(expected_session=expected.isoformat(), universe=len(canonical), fresh=0)
    for security_id, ticker in sorted(canonical.items(), key=lambda kv: kv[1]):
        last = _last_bar(research_db, security_id)
        behind = sessions_behind(date.fromisoformat(last) if last else None, expected)
        if behind == 0:
            result.fresh += 1
            continue

        attempt = _last_attempt(experiment_db, ticker)
        classification: str | None = None
        notes = None

        if last is None:
            if symbol_has_evidence(research_db, ticker) is False:
                classification = INVALID_SYMBOL  # never resolvable; nothing to fetch
            else:
                classification = CHECKPOINT_LOST
        elif ticker.upper() in retired:
            classification = DELISTED_EMPTY
        elif behind <= window_sessions:
            # Inside the documented rolling-coverage window: the rotation is
            # allowed to leave a cohort name for the next pass. Reported as
            # "lagging within window", not an incident, and never remediated
            # (it would burn provider quota on names the rolling design is
            # already tracking).
            result.lagging_within_window.append(ticker)
            continue
        else:
            # Materially stale: past the contract the refresh itself claims.
            classification = None
            if attempt:
                classification = _classify_from_attempt(attempt.get("error"), attempt.get("status"))
                # "Fetched but not stored" needs the attempt to have run AFTER
                # the expected session's publication boundary -- otherwise the
                # SUCCESS belongs to an older session and proves nothing.
                if (
                    classification is None
                    and attempt.get("status") == "SUCCESS"
                    and str(attempt.get("requested_at") or "")[:10] >= expected.isoformat()
                ):
                    classification = VALIDATION_FAILURE
            if classification is None:
                classification = ROTATION_STARVED if budget_starved else INTERRUPTED_BATCH

        if classification == ROTATION_STARVED and notes is None:
            notes = (
                f"refresh budget {budget}/day cannot cover {len(canonical)} symbols "
                f"within the {REFRESH_STALENESS_DAYS}-day window (needs {required_daily}/day)"
            )

        row = SecurityFreshness(
            ticker=ticker,
            security_id=security_id,
            last_bar=last,
            missing_sessions=max(behind, 0),
            classification=classification,
            last_attempt_at=(attempt or {}).get("requested_at"),
            last_attempt_status=(attempt or {}).get("status"),
            last_attempt_http=(attempt or {}).get("http_status"),
            last_attempt_error=(attempt or {}).get("error"),
            notes=notes,
        )
        if classification in LEGITIMATE_EXCEPTIONS:
            result.exceptions.append(row)
        else:
            result.stale.append(row)
            result.groups.setdefault(classification, []).append(ticker)
    return result


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------
def backoff_seconds(attempts: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter, capped. ``attempts`` is 1-based."""
    rng = rng or random
    base = min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_SECONDS)
    return base + rng.uniform(0.0, base * BACKOFF_JITTER_FRACTION)


def _next_attempt_at(attempts: int, now: datetime, rng: random.Random | None = None) -> str:
    return (now + timedelta(seconds=backoff_seconds(attempts, rng=rng))).isoformat()


def remediate(
    *,
    settings,
    experiment_db,
    paths: ResearchPaths | None = None,
    audit: AuditResult | None = None,
    store: CheckpointStore | None = None,
    as_of: date | None = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
    adapter=None,
    refresh_one=None,
    max_attempts_per_run: int = MAX_ATTEMPTS_PER_RUN,
    max_attempts_total: int = MAX_ATTEMPTS_TOTAL,
    window_hours: int = REMEDIATION_WINDOW_HOURS,
    time_budget_seconds: float | None = None,
) -> dict:
    """Bounded, idempotent repair of the stale set. Returns a summary.

    Only the affected symbols are re-fetched -- never the whole universe. The
    provider quota object enforces the rate limits; this function never raises
    concurrency to work around throttling.
    """
    from tradehub_research.backfill.tiingo_driver import classify_error, fetch_one
    from tradehub_research.db import ResearchDB
    from tradehub_research.evidence import EvidenceStore
    from tradehub_research.ops.daily_refresh import INCREMENTAL_LOOKBACK_SESSIONS
    from tradehub_research.adapters.tiingo import TiingoEodAdapter

    paths = paths or research_paths()
    now = now or datetime.now(timezone.utc)
    rng = rng or random.Random(0)
    expected = date.fromisoformat((audit.expected_session if audit else (as_of or expected_latest_session(now)).isoformat()))
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    store = store or CheckpointStore(paths.research_dir / "freshness_remediation.sqlite")

    if audit is None:
        audit = audit_universe(settings=settings, paths=paths, experiment_db=experiment_db, as_of=expected)

    run_key = run_key_for(expected)
    store.open_run(run_key, expected.isoformat(), audit.universe, audit.stale_count)
    for row in audit.stale:
        store.seed_symbol(run_key, row.ticker, row.security_id, row.last_bar, row.classification)
    for row in audit.exceptions:
        store.seed_symbol(run_key, row.ticker, row.security_id, row.last_bar, row.classification)
        store.settle(
            run_key, row.ticker, disposition="EXCLUDED", last_bar_after=row.last_bar,
            classification=row.classification,
        )

    if adapter is None and refresh_one is None:
        adapter = TiingoEodAdapter(
            token=settings.tiingo_token,
            license_confirmed=settings.tiingo_license_confirmed,
            user_agent="TigerTradeHub ops-freshness-remediate",
            cache_dir=settings.adapter_cache_dir,
            cache_budget_bytes=2 * 1024 * 1024 * 1024,
        )
    store_evidence = EvidenceStore(research_db)

    summary = {
        "run_key": run_key,
        "expected_session": expected.isoformat(),
        "targeted": len(audit.stale),
        "repaired": 0,
        "excluded": len(audit.exceptions),
        "unresolved": 0,
        "attempts": 0,
        "quota_blocked": False,
    }

    for row in store.pending(run_key, now=now):
        if time_budget_seconds is not None:
            elapsed = (datetime.now(timezone.utc) - now).total_seconds()
            if elapsed >= time_budget_seconds:
                # Bounded recovery: stop cleanly rather than sit inside the
                # provider's hourly quota window. Everything stays PENDING with
                # its attempt count intact, so the next cycle resumes exactly
                # here -- and the report is emitted now instead of hours later.
                summary["time_budget_exhausted"] = True
                break
        ticker = str(row["ticker"])
        attempts_so_far = int(row["attempts"])
        if adapter is not None and refresh_one is None:
            # Pre-flight the provider budget. `fetch_one` will otherwise sleep
            # inside the hourly quota window (up to 12h) for EVERY remaining
            # symbol, which is how a nightly watch becomes an all-night job.
            # Stopping here leaves the queue intact and reports immediately.
            try:
                remaining = adapter.quota.remaining(datetime.now(timezone.utc).timestamp())
            except Exception:  # noqa: BLE001 -- never let introspection break the run
                remaining = None
            if remaining is not None and remaining.get("hourly", 1) <= 0:
                summary["quota_blocked"] = True
                break
        if attempts_so_far >= max_attempts_total:
            store.settle(
                run_key, ticker, disposition="UNRESOLVED",
                last_bar_after=_last_bar(research_db, row["security_id"] or ""),
                classification=UNRESOLVED,
            )
            summary["unresolved"] += 1
            continue
        started = datetime.fromisoformat(str(row["last_attempt_at"] or now.isoformat()))
        if (now - started) > timedelta(hours=window_hours):
            store.settle(
                run_key, ticker, disposition="UNRESOLVED",
                last_bar_after=_last_bar(research_db, row["security_id"] or ""),
                classification=UNRESOLVED,
            )
            summary["unresolved"] += 1
            continue

        outcome = "FAILED"
        error: str | None = None
        attempts_total = attempts_so_far
        for attempt in range(1, max_attempts_per_run + 1):
            summary["attempts"] += 1
            try:
                if refresh_one is not None:
                    refresh_one(ticker)
                else:
                    fetched = fetch_one(
                        adapter,
                        adapter.quota,
                        ticker=ticker,
                        start_date=(expected - timedelta(days=INCREMENTAL_LOOKBACK_SESSIONS * 2)).isoformat(),
                        end_date=expected.isoformat(),
                    )
                    records = adapter.parse(fetched.raw_bytes, fetched, ticker=ticker)
                    if not records:
                        outcome = "EMPTY"
                        break
                    from tradehub_research.adapters.base import ingest_records

                    ingest_records(records, store_evidence)
                outcome = "FETCHED"
                break
            except Exception as exc:  # noqa: BLE001 -- classify, never crash the run
                cls, detail, _http = classify_error(exc)
                error = f"{cls}: {detail}"
                if cls == "QUOTA":
                    # The provider refused the request before it was made; this
                    # is a budget limit, NOT a failure of this symbol. Recording
                    # it as an attempt would drive the whole remaining queue
                    # toward a false UNRESOLVED escalation without a single
                    # request being sent. Leave the symbol PENDING and stop.
                    summary["quota_blocked"] = True
                    outcome = "QUOTA"
                    break
                # Every provider request counts against the durable budget:
                # "retry budget" is a limit on attempts actually spent, which is
                # also what the provider quota sees.
                attempts_total = store.record_attempt(
                    run_key, ticker, error=error,
                    next_attempt_at=_next_attempt_at(attempts_so_far + 1, now, rng=rng),
                )
                if cls in ("AUTH", "UNKNOWN_SYMBOL", "DUPLICATE_CIK"):
                    break  # not transient; do not burn quota on it
                if attempts_total >= max_attempts_total:
                    break  # budget spent -- escalate rather than keep retrying
                if attempt < max_attempts_per_run:
                    continue  # bounded immediate retry inside this run

        if outcome == "QUOTA":
            # Resume next cycle: the checkpoint keeps this symbol PENDING with
            # its attempt count untouched.
            break

        after = _last_bar(research_db, row["security_id"] or "")
        verified = bool(after) and date.fromisoformat(after) >= expected
        if outcome == "EMPTY":
            store.record_attempt(
                run_key, ticker, error="EMPTY: 0 bars parsed (delisted/unresolvable)",
                next_attempt_at=None,
            )
            store.settle(
                run_key, ticker, disposition="EXCLUDED", last_bar_after=after,
                classification=DELISTED_EMPTY,
            )
            summary["excluded"] += 1
            continue
        if verified:
            store.settle(run_key, ticker, disposition="REPAIRED", last_bar_after=after)
            summary["repaired"] += 1
            continue

        if attempts_total < max_attempts_total:
            attempts_total = store.record_attempt(
                run_key, ticker,
                error=error or f"{VALIDATION_FAILURE}: bar missing after a successful fetch",
                next_attempt_at=_next_attempt_at(attempts_total + 1, now, rng=rng),
            )
        if attempts_total >= max_attempts_total:
            store.settle(run_key, ticker, disposition="UNRESOLVED", last_bar_after=after)
            summary["unresolved"] += 1

    remaining = [
        r for r in store.all_symbols(run_key) if r["disposition"] == "PENDING"
    ]
    summary["pending"] = len(remaining)
    summary["requires_intervention"] = summary["unresolved"] > 0 or bool(remaining)
    store.close_run(run_key, "ESCALATED" if summary["requires_intervention"] else "COMPLETED")
    return summary


def verify(
    *,
    settings,
    experiment_db=None,
    paths: ResearchPaths | None = None,
    run_key: str | None = None,
    as_of: date | None = None,
    store: CheckpointStore | None = None,
) -> dict:
    """Post-repair verification. Never trusts an exit code."""
    from tradehub_research.db import ResearchDB

    paths = paths or research_paths()
    expected = as_of or expected_latest_session()
    run_key = run_key or run_key_for(expected)
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    store = store or CheckpointStore(paths.research_dir / "freshness_remediation.sqlite")
    rows = store.all_symbols(run_key)

    repaired_ok = 0
    still_stale: list[dict] = []
    duplicates: list[str] = []
    for row in rows:
        if row["disposition"] != "REPAIRED":
            continue
        after = _last_bar(research_db, row["security_id"] or "")
        if after and date.fromisoformat(after) >= expected:
            repaired_ok += 1
        else:
            still_stale.append({"ticker": row["ticker"], "last_bar": after, "reason": "regressed after repair"})
        with research_db.connect(read_only=True) as conn:
            dupes = conn.execute(
                "SELECT COUNT(*) FROM (SELECT source_record_id FROM evidence_event "
                "WHERE security_id=? AND source_id='tiingo_eod' GROUP BY source_record_id "
                "HAVING COUNT(*) > 1)",
                (row["security_id"],),
            ).fetchone()[0]
        if dupes:
            duplicates.append(str(row["ticker"]))

    return {
        "run_key": run_key,
        "expected_session": expected.isoformat(),
        "repaired_verified": repaired_ok,
        "regressed": still_stale,
        "duplicate_symbols": duplicates,
        "checkpoint_consistent": store.consistent_completed(run_key),
        "verified_at": _utc_now(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from tradehub_research.config import ResearchSettings
    from tradehub_research.validation.experiment_db import ExperimentDB

    argv = argv if argv is not None else sys.argv[1:]
    mode = argv[0] if argv else "audit"
    settings = ResearchSettings()
    paths = research_paths()
    exp = ExperimentDB(paths.experiment_db)

    if mode == "audit":
        audit = audit_universe(settings=settings, paths=paths, experiment_db=exp)
        print(json.dumps(audit.as_dict(), indent=2, sort_keys=True))
        return 0
    if mode == "remediate":
        summary = remediate(settings=settings, experiment_db=exp, paths=paths)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    if mode == "verify":
        print(json.dumps(verify(settings=settings, experiment_db=exp, paths=paths), indent=2, sort_keys=True))
        return 0
    print(f"usage: {argv0()} [audit|remediate|verify]", file=sys.stderr)
    return 2


def argv0() -> str:
    return "python -m tradehub_research.ops.data_freshness"


def incident_id(expected_session: str, tickers: list[str]) -> str:
    """Stable identifier so a re-run of the same incident does not double-log."""
    material = expected_session + "|" + "|".join(sorted(tickers))
    return hashlib.sha256(material.encode()).hexdigest()[:16]


if __name__ == "__main__":
    raise SystemExit(main())

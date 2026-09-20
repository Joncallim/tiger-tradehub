"""Daily market/evidence refresh (issue #39 B2).

Deterministic incremental refresh, bounded by the Tiingo Starter quota
(45 req/hr reserve; ~900/day). Design:

- ACTIVE set (securities with recent production screens) is refreshed
  every run -- the funnel always sees fresh data for names it cares about.
- The remaining daily request budget refreshes the cohort ROTATIONALLY
  (ticker-ascending, skipping symbols whose last bar already covers the
  last completed US session) so the whole cohort rolls over every few days
  without rerunning the historical bootstrap.
- SEC: per-CIK companyfacts for cohort CIKs with NO entity-level facts OR
  with facts older than the freshness horizon (bounded, one request each).
- Corporate actions ride the EOD annotations (dividend/split rows).
- Everything is resume-safe via the ingested-evidence oracle; every attempt
  is recorded append-only in the backfill ledger.

Exit codes: 0 = ok (possibly with SKIPPED), 2 = quota-exhausted mid-run
(resume next tick), 3 = fatal.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradehub_research.adapters.base import ingest_records
from tradehub_research.adapters.tiingo import TiingoEodAdapter, TiingoQuota
from tradehub_research.backfill.tiingo_driver import (
    canonical_tickers_by_cik,
    classify_error,
    fetch_one,
    record_attempt,
    symbol_has_evidence,
)
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session, research_paths
from tradehub_research.validation.experiment_db import ExperimentDB

INCREMENTAL_LOOKBACK_SESSIONS = 10
ACTIVE_SET_MAX_REQUESTS = 60
#: Floor for the rotation. The EFFECTIVE budget is computed per run from the
#: universe size and the rolling window (see `rotation_budget_for`) so the
#: refresh cannot advertise a freshness contract it is arithmetically unable to
#: deliver -- the 2026-09-19 incident: 40/day against 443 names inside a
#: 6-session window (needs 74/day), leaving a 323-name cohort permanently past
#: the contract.
ROTATION_REQUESTS_PER_RUN = 40
#: Hard ceiling so a runaway universe cannot turn the rotation into a hammer.
#: The provider quota (45/hr, 900/day reserve) still gates every request.
ROTATION_REQUESTS_MAX = 200
REFRESH_STALENESS_DAYS = 7
# A symbol whose fetch returns 0 bars despite a data gap this long is treated
# as delisted/unresolvable (Tiingo returns 200-with-empty for delisted names).
RETIRE_GAP_DAYS = 14
RETIRED_FILE = Path("/var/lib/tradehub-research/autonomy/retired_securities.json")


def rotation_budget_for(
    universe: int, *, window_sessions: int | None = None, as_of: date | None = None
) -> int:
    """Rotation requests needed per run to hold the freshness contract.

    Converts the design's calendar-day window (REFRESH_STALENESS_DAYS) into the
    sessions inside it -- the unit a market-data contract is actually measured
    in -- then returns ceil(universe / window_sessions), clamped to
    [ROTATION_REQUESTS_PER_RUN, ROTATION_REQUESTS_MAX].
    """
    from tradehub_research.ops.market_calendar import count_sessions, expected_latest_session

    as_of = as_of or expected_latest_session()
    if window_sessions is None:
        window_sessions = max(
            count_sessions(as_of - timedelta(days=REFRESH_STALENESS_DAYS), as_of), 1
        )
    required = -(-universe // window_sessions)
    return max(ROTATION_REQUESTS_PER_RUN, min(required, ROTATION_REQUESTS_MAX))


def _sessions_behind(last: str | None, as_of: date) -> int:
    """Expected sessions strictly after ``last`` up to ``as_of`` (-1 = no bars)."""
    from tradehub_research.ops.market_calendar import sessions_behind

    if last is None:
        return -1
    return sessions_behind(date.fromisoformat(str(last)[:10]), as_of)


def _load_retired() -> set[str]:
    """Tickers the refresh has retired as delisted/unresolvable."""
    if not RETIRED_FILE.exists():
        return set()
    try:
        data = json.loads(RETIRED_FILE.read_text())
    except (ValueError, OSError):
        return set()
    return {str(item.get("ticker", "")).upper() for item in data if isinstance(item, dict)}


def retired_tickers() -> set[str]:
    """Public read-only accessor (health + watch exclude these from staleness)."""
    return _load_retired()


def _retire(ticker: str, last_bar: str | None, reason: str) -> None:
    """Record a ticker as retired (idempotent, append-only file)."""
    RETIRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        items = json.loads(RETIRED_FILE.read_text()) if RETIRED_FILE.exists() else []
    except (ValueError, OSError):
        items = []
    existing = {str(item.get("ticker", "")).upper() for item in items if isinstance(item, dict)}
    if ticker.upper() in existing:
        return
    items.append(
        {
            "ticker": ticker.upper(),
            "last_bar": last_bar,
            "reason": reason,
            "retired_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    RETIRED_FILE.write_text(json.dumps(items, sort_keys=True, indent=2) + "\n")


def _active_securities(research_db: ResearchDB, days: int = 14) -> set[str]:
    """Securities with production screens (any family) in the last N days."""
    cutoff = utc_now()[:10]
    with research_db.connect(read_only=True) as conn:
        try:
            rows = conn.execute(
                "SELECT DISTINCT sr.security_id FROM screen_result sr "
                "JOIN screen_definition d ON d.config_hash = sr.config_hash "
                "WHERE date(sr.computed_at) >= date(?, ?)",
                (cutoff, f"-{days} days"),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- older schemas may lack computed_at
            rows = conn.execute("SELECT DISTINCT security_id FROM screen_result").fetchall()
    return {str(r[0]) for r in rows}


def _last_bar_date(research_db: ResearchDB, security_id: str) -> str | None:
    with research_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT MAX(json_extract(structured_fields, '$.session_date')) AS d "
            "FROM evidence_event WHERE security_id=? AND source_id='tiingo_eod'",
            (security_id,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


def _needs_fresh(research_db: ResearchDB, security_id: str, as_of: str) -> bool:
    last = _last_bar_date(research_db, security_id)
    return last is None or last < as_of


def _maybe_retire(research_db: ResearchDB, ticker: str) -> None:
    """Retire a ticker whose fetch returned 0 bars AND whose data is already
    older than RETIRE_GAP_DAYS (a delisted name, not a transient gap)."""
    try:
        with research_db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT MAX(json_extract(structured_fields, '$.session_date')) AS d "
                "FROM evidence_event e JOIN security s ON s.security_id=e.security_id "
                "WHERE upper(s.canonical_ticker)=upper(?) AND e.source_id='tiingo_eod'",
                (ticker,),
            ).fetchone()
        last_bar = row["d"] if row and row["d"] else None
    except Exception:  # noqa: BLE001 -- never fatal to the refresh
        return
    if last_bar is None:
        return
    try:
        if (date.today() - date.fromisoformat(str(last_bar)[:10])).days > RETIRE_GAP_DAYS:
            _retire(ticker, last_bar, "fetch returns 0 bars; data older than the retire gap")
    except ValueError:
        return


def _refresh_one(
    adapter: TiingoEodAdapter,
    quota: TiingoQuota,
    research_db: ResearchDB,
    experiment_db: ExperimentDB,
    store: EvidenceStore,
    ticker: str,
    as_of: str,
    summary: dict[str, int],
) -> None:
    try:
        fetched = fetch_one(
            adapter,
            quota,
            ticker=ticker,
            start_date=(as_of - timedelta(days=INCREMENTAL_LOOKBACK_SESSIONS * 2)).isoformat(),
            end_date=as_of.isoformat(),
        )
        records = adapter.parse(fetched.raw_bytes, fetched, ticker=ticker)
        if not records:
            # 200 with zero bars = the ticker no longer has EOD data (delisted).
            # Recorded as ERROR (the attempt yielded no usable data) with a
            # descriptive class; the summary tracks EMPTY separately for the
            # daily report.
            record_attempt(
                experiment_db,
                ticker=ticker,
                status="ERROR",
                http_status=fetched.status,
                bytes_count=len(fetched.raw_bytes),
                error="EMPTY: 0 bars parsed (delisted/unresolvable)",
            )
            summary["EMPTY"] += 1
            _maybe_retire(research_db, ticker)
            return
        ids = ingest_records(records, store)
        record_attempt(
            experiment_db,
            ticker=ticker,
            status="SUCCESS",
            http_status=fetched.status,
            bytes_count=len(fetched.raw_bytes),
            error=None,
        )
        summary["SUCCESS"] += 1
        summary["records"] += len(ids)
    except Exception as exc:  # noqa: BLE001
        cls, detail, http = classify_error(exc)
        record_attempt(
            experiment_db,
            ticker=ticker,
            status="ERROR",
            http_status=http,
            bytes_count=0,
            error=f"{cls}: {detail}",
        )
        summary["ERROR"] += 1
        if cls == "QUOTA":
            raise


def run_daily_refresh(
    *,
    settings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    as_of=None,
    active_max: int = ACTIVE_SET_MAX_REQUESTS,
    rotation_budget: int = ROTATION_REQUESTS_PER_RUN,
) -> dict:
    """Run one bounded daily refresh tick. Returns the summary dict."""

    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    as_of = as_of or last_completed_us_session()

    adapter = TiingoEodAdapter(
        token=settings.tiingo_token,
        license_confirmed=settings.tiingo_license_confirmed,
        user_agent="TigerTradeHub ops-daily-refresh",
        cache_dir=settings.adapter_cache_dir,
        cache_budget_bytes=2 * 1024 * 1024 * 1024,
    )
    quota = adapter.quota
    store = EvidenceStore(research_db)
    canonical = canonical_tickers_by_cik(research_db)  # security_id -> ticker
    by_ticker = {ticker: sid for sid, ticker in canonical.items()}

    summary: dict = {
        "as_of": as_of.isoformat(),
        "SUCCESS": 0,
        "ERROR": 0,
        "EMPTY": 0,
        "SKIPPED_FRESH": 0,
        "records": 0,
        "active_refreshed": 0,
        "rotation_refreshed": 0,
    }
    try:
        # 1. Active set first: securities with recent production screens.
        active = _active_securities(research_db) & set(by_ticker)
        for ticker in sorted(active)[:active_max]:
            sid = by_ticker[ticker]
            if not _needs_fresh(research_db, sid, as_of.isoformat()):
                summary["SKIPPED_FRESH"] += 1
                continue
            _refresh_one(adapter, quota, research_db, experiment_db, store, ticker, as_of, summary)
            summary["active_refreshed"] += 1
        # 2. Rotation: cohort symbols not refreshed within the rolling window.
        #    The window is measured in SESSIONS, not calendar days: weekends and
        #    holidays must never count against a symbol, and the budget is sized to
        #    the universe so the contract is achievable by construction.
        rotated = 0
        retired = _load_retired()
        from tradehub_research.ops.market_calendar import count_sessions
        from tradehub_research.ops.symbol_capacity import plan_symbol_capacity

        window_sessions = max(
            count_sessions(as_of - timedelta(days=REFRESH_STALENESS_DAYS), as_of), 1
        )
        if rotation_budget is None or rotation_budget == ROTATION_REQUESTS_PER_RUN:
            rotation_budget = rotation_budget_for(
                len(by_ticker), window_sessions=window_sessions, as_of=as_of
            )
        summary["rotation_budget"] = rotation_budget
        summary["window_sessions"] = window_sessions

        # Which symbols actually need a refresh? Decided before any request so
        # the capacity plan below sees the true demand.
        candidates: list[str] = []
        for ticker in sorted(by_ticker):
            if ticker.upper() in retired:
                continue  # delisted/unresolvable -- no longer fetched
            sid = by_ticker[ticker]
            last = _last_bar_date(research_db, sid)
            if last is not None and _sessions_behind(last, as_of) <= window_sessions:
                summary["SKIPPED_FRESH"] += 1
                continue
            if symbol_has_evidence(research_db, ticker) is False:
                continue  # never resolvable (UNKNOWN_SYMBOL) -- leave for the ledger
            candidates.append(ticker)

        # Rolling-month symbol capacity, planned BEFORE spending. A symbol already
        # inside the window consumes no new capacity, so a set at 450/450 still
        # refreshes everything the fleet already owns; only genuinely new symbols
        # can be deferred, and they are reported instead of being silently
        # dropped. This is what stops the ceiling being discovered by hitting it.
        plan = plan_symbol_capacity(quota, candidates, now=time.time(), active=sorted(active))
        summary["symbol_capacity"] = plan.as_dict()
        if plan.deferred:
            summary["symbol_capacity_deferred"] = plan.deferred[:50]
            summary["symbol_capacity_deferred_count"] = len(plan.deferred)
        admitted = set(plan.already_reserved) | set(plan.admissible_new)

        for ticker in candidates:
            if rotated >= rotation_budget:
                break
            if ticker.upper() not in admitted:
                continue  # deferred by capacity; reported in the summary
            _refresh_one(adapter, quota, research_db, experiment_db, store, ticker, as_of, summary)
            summary["rotation_refreshed"] += 1
            rotated += 1
    except RuntimeError as exc:
        if "quota" in str(exc):
            summary["status"] = "QUOTA_EXHAUSTED"
            return summary
        raise
    summary["status"] = "OK"
    return summary


def main(argv: list[str] | None = None) -> int:
    from tradehub_research.config import ResearchSettings

    settings = ResearchSettings()
    experiment_db = ExperimentDB(research_paths().experiment_db)
    summary = run_daily_refresh(settings=settings, experiment_db=experiment_db)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary.get("status") == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())

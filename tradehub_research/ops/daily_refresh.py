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
from datetime import timedelta

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
ROTATION_REQUESTS_PER_RUN = 40
REFRESH_STALENESS_DAYS = 7


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
        ids = ingest_records(records, store)
        record_attempt(
            experiment_db,
            ticker=ticker,
            status="SUCCESS",
            http_status=fetched.http_status,
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
        # 2. Rotation: cohort symbols not refreshed within REFRESH_STALENESS_DAYS.
        rotated = 0
        for ticker in sorted(by_ticker):
            if rotated >= rotation_budget:
                break
            sid = by_ticker[ticker]
            last = _last_bar_date(research_db, sid)
            if (
                last is not None
                and last >= (as_of - timedelta(days=REFRESH_STALENESS_DAYS)).isoformat()
            ):
                summary["SKIPPED_FRESH"] += 1
                continue
            if symbol_has_evidence(research_db, ticker) is False:
                continue  # never resolvable (UNKNOWN_SYMBOL) -- leave for the ledger
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

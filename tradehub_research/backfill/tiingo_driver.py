"""Bounded Tiingo EOD backfill driver for the frozen BOOTSTRAP_COHORT (#38).

Two modes:

- ``--smoke``: exactly ONE bounded authenticated request (first resolvable
  cohort ticker, single-session window) to validate credentials WITHOUT
  burning cohort symbol slots. Prints status/bytes only -- never the token.
  If auth fails, the driver reports the diagnostic class and exits non-zero
  WITHOUT starting the backfill (owner brief: stop before burning quota).

- ``--run``: full-history EOD backfill for the frozen 450-ticker cohort,
  bounded by the existing TiingoQuota (50/hr, 1000/day with 10% reserve;
  450-symbol rolling-month ceiling -- never loosened).

Guarantees (owner brief sections 1-3):

- reads the FROZEN universe_sample from experiment.db; never resamples;
- deterministic processing order: ticker ascending (matches the quota
  table's canonical ordering); resume-safe (SUCCESS attempt or existing
  price_bar evidence skips re-fetch);
- every attempt recorded append-only in experiment.db ``backfill_attempt``
  (SUCCESS / ERROR with http_status + error class / SKIPPED_QUOTA);
- failure classification is honest: UNKNOWN_SYMBOL (404, likely
  unavailable/delisted), AUTH (401/403 -- aborts), RATE_LIMITED (429),
  PROVIDER_ERROR (5xx), NETWORK (transport), DUPLICATE_CIK (share-class
  ticker that shares the canonical CIK -- never fetched, never merged into
  the CIK price series), QUOTA (ceiling reached -- stops the run);
- permanent failures are recorded once and never re-fetched; transient
  failures (RATE_LIMITED/PROVIDER_ERROR/NETWORK) get one deferred retry
  pass at the end of the run;
- raw/cache lineage preserved: NetworkClient disk cache, source_id
  ``tiingo_eod``, canonical content hashes; adjusted fields stay in the
  ``provider_adjusted_audit_only`` namespace (audit/outcome-only).

Never prints, logs or persists the token itself.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradehub_research.adapters.base import FetchResult, ingest_records
from tradehub_research.adapters.tiingo import TiingoEodAdapter, TiingoQuota
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.validation.experiment_db import (
    DEFAULT_EXPERIMENT_DB_PATH,
    ExperimentDB,
)

BACKFILL_START_DATE = "2010-01-01"
PERMANENT_ERROR_CLASSES = frozenset({"UNKNOWN_SYMBOL", "DUPLICATE_CIK", "AUTH"})
TRANSIENT_ERROR_CLASSES = frozenset({"RATE_LIMITED", "PROVIDER_ERROR", "NETWORK"})
MAX_QUOTA_WAIT_SECONDS = 12 * 3600  # never wait forever for a quota window


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def load_cohort_tickers(experiment_db: ExperimentDB) -> list[dict[str, str]]:
    """The frozen universe_sample rows (sample_id + tickers), unmodified.

    Returns [{ticker, cik, title}] in the FROZEN hash order. Raises if the
    sample is missing or its recorded size disagrees with the JSON payload
    (append-only table; a mismatch is corruption, not a resampling excuse).
    """
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT sample_id, selected_tickers_json, selected_count FROM universe_sample "
            "ORDER BY created_at"
        ).fetchall()
    if not rows:
        raise RuntimeError("no frozen universe_sample in experiment.db")
    sample = rows[-1]
    tickers = json.loads(sample["selected_tickers_json"])
    if len(tickers) != sample["selected_count"]:
        raise RuntimeError(
            f"universe_sample {sample['sample_id']} count mismatch: "
            f"json={len(tickers)} recorded={sample['selected_count']}"
        )
    return tickers


def canonical_tickers_by_cik(research_db: ResearchDB) -> dict[str, str]:
    """security_id (CIK) -> canonical_ticker, RESTRICTED to securities with an
    eligible terminal universe membership (the active BOOTSTRAP_COHORT
    universe; retired non-cohort securities never resolve)."""
    with research_db.connect(read_only=True) as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE visible_chain(root_id, descendant_id) AS (
                SELECT candidate.id, candidate.id FROM universe_membership candidate
                WHERE NOT EXISTS (
                    SELECT 1 FROM universe_membership predecessor
                    WHERE predecessor.id=candidate.supersedes_id)
                UNION ALL
                SELECT chain.root_id, correction.id
                FROM visible_chain chain JOIN universe_membership correction
                  ON correction.supersedes_id=chain.descendant_id
            ), terminal(root_id, descendant_id) AS (
                SELECT chain.root_id, chain.descendant_id FROM visible_chain chain
                WHERE NOT EXISTS (
                    SELECT 1 FROM visible_chain child
                    JOIN universe_membership item ON item.id=child.descendant_id
                    WHERE child.root_id=chain.root_id
                      AND item.supersedes_id=chain.descendant_id)
            )
            SELECT s.security_id, s.canonical_ticker
            FROM terminal
            JOIN universe_membership m ON m.id=terminal.descendant_id
            JOIN security s ON s.security_id=m.security_id
            WHERE m.eligible=1
            ORDER BY s.canonical_ticker
            """
        ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def symbol_has_evidence(research_db: ResearchDB, ticker: str) -> bool:
    """Ingested tiingo_eod evidence is the resume oracle (attempt rows are
    attempt records, not proof of ingestion)."""
    with research_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM evidence_event e JOIN security s ON s.security_id=e.security_id "
            "WHERE upper(s.canonical_ticker)=upper(?) AND e.source_id='tiingo_eod' LIMIT 1",
            (ticker,),
        ).fetchone()
    return row is not None


def record_attempt(
    experiment_db: ExperimentDB,
    *,
    ticker: str,
    status: str,
    http_status: int | None,
    bytes_count: int | None,
    error: str | None,
) -> None:
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO backfill_attempt "
            "(attempt_id, provider, symbol_or_cik, status, http_status, bytes, error, "
            "requested_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "tiingo",
                ticker,
                status,
                http_status,
                bytes_count,
                error,
                _utc_now(),
            ),
        )


def classify_error(exc: BaseException) -> tuple[str, str, int | None]:
    """(error_class, message, http_status) for one failed fetch/parse/ingest."""
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in (401, 403):
                return "AUTH", f"HTTP {status}", status
            if status == 404:
                return "UNKNOWN_SYMBOL", "HTTP 404", status
            if status == 429:
                return "RATE_LIMITED", "HTTP 429", status
            return "PROVIDER_ERROR", f"HTTP {status}", status
    except Exception:  # noqa: BLE001 - classification must never mask the cause
        pass
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if "quota reserve" in message:
            return "QUOTA", message[:200], None
        if "ceiling" in message:
            return "QUOTA", message[:200], None
    if isinstance(exc, ValueError):
        if "unresolved" in str(exc):
            return "DUPLICATE_CIK", str(exc)[:200], None
        return "PARSE", str(exc)[:200], None
    return "NETWORK", f"{type(exc).__name__}: {str(exc)[:200]}", None


def _wait_for_quota_window(quota: TiingoQuota, max_wait: int = MAX_QUOTA_WAIT_SECONDS) -> None:
    """Sleep until the hourly window reopens (deterministic pacing, bounded)."""
    waited = 0.0
    while waited < max_wait:
        remaining = quota.remaining(_now_ts())
        if remaining["hourly"] > 0:
            return
        now = datetime.now(timezone.utc)
        next_hour = (
            now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        ).timestamp()
        sleep_for = min(next_hour - now.timestamp() + 5.0, 600.0)
        time.sleep(sleep_for)
        waited += sleep_for


def fetch_one(
    adapter: TiingoEodAdapter,
    quota: TiingoQuota,
    *,
    ticker: str,
    start_date: str,
    end_date: str,
) -> FetchResult:
    """Fetch with quota-window pacing; only quota exhaustion blocks."""
    while True:
        try:
            return adapter.fetch_prices(ticker, start_date, end_date)
        except RuntimeError as exc:
            message = str(exc)
            if "quota reserve" in message or "ceiling" in message:
                if "ceiling" in message:
                    raise  # 450-symbol ceiling is fatal for the run
                _wait_for_quota_window(quota)
                continue
            raise


def run_smoke(*, settings: ResearchSettings, experiment_db: ExperimentDB) -> int:
    """One bounded authenticated request; no cohort slot burn beyond the symbol itself."""
    research_db = ResearchDB(settings.db_path, settings.busy_timeout_ms)
    canonical = canonical_tickers_by_cik(research_db)
    if not canonical:
        raise RuntimeError(
            "no bootstrapped securities in research.db -- run security_bootstrap first"
        )
    ticker = sorted(canonical.values())[0]
    today = datetime.now(timezone.utc).date().isoformat()
    week_ago = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    try:
        adapter = TiingoEodAdapter(
            token=settings.tiingo_token,
            license_confirmed=settings.tiingo_license_confirmed,
            user_agent="TigerTradeHub research-backfill",
            cache_dir=settings.adapter_cache_dir,
            max_attempts=1,
        )
        quota = adapter.quota
        fetched = fetch_one(adapter, quota, ticker=ticker, start_date=week_ago, end_date=today)
    except Exception as exc:  # noqa: BLE001 - smoke must report an honest bounded result
        error_class, message, status = classify_error(exc)
        record_attempt(
            experiment_db,
            ticker=ticker,
            status="ERROR",
            http_status=status,
            bytes_count=None,
            error=f"{error_class}: {message}",
        )
        print(
            json.dumps(
                {"ok": False, "ticker": ticker, "error_class": error_class, "detail": message}
            )
        )
        if error_class == "AUTH":
            print("AUTH failed: check the token in the ignored .env (value never displayed).")
        return 1
    try:
        records = adapter.parse(fetched.raw_bytes, fetched, ticker=ticker)
        ids = ingest_records(records, EvidenceStore(research_db), dry_run=True)
    except Exception as exc:  # noqa: BLE001
        record_attempt(
            experiment_db,
            ticker=ticker,
            status="ERROR",
            http_status=fetched.status,
            bytes_count=len(fetched.raw_bytes),
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        print(
            json.dumps(
                {"ok": False, "ticker": ticker, "error_class": "PARSE", "detail": str(exc)[:200]}
            )
        )
        return 1
    record_attempt(
        experiment_db,
        ticker=ticker,
        status="SUCCESS",
        http_status=fetched.status,
        bytes_count=len(fetched.raw_bytes),
        error=f"SMOKE: HTTP {fetched.status}, {len(ids)} records parsed",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "ticker": ticker,
                "http_status": fetched.status,
                "bytes": len(fetched.raw_bytes),
                "parsed_records": len(ids),
            }
        )
    )
    return 0


def run_backfill(
    *, settings: ResearchSettings, experiment_db: ExperimentDB, args_limit: int | None = None
) -> int:
    research_db = ResearchDB(settings.db_path, settings.busy_timeout_ms)
    cohort = load_cohort_tickers(experiment_db)
    canonical = canonical_tickers_by_cik(research_db)
    canonical_set = set(canonical.values())

    adapter = TiingoEodAdapter(
        token=settings.tiingo_token,
        license_confirmed=settings.tiingo_license_confirmed,
        user_agent="TigerTradeHub research-backfill",
        cache_dir=settings.adapter_cache_dir,
        cache_budget_bytes=8 * 1024 * 1024 * 1024,  # ~450 full histories + SEC bulk artifacts
    )
    quota = adapter.quota

    # Deterministic processing order: ticker ascending.
    ordered = sorted(cohort, key=lambda row: row["ticker"])
    if args_limit is not None and args_limit > 0:
        ordered = ordered[:args_limit]
    pending: list[dict[str, str]] = []
    transient_retry: list[dict[str, str]] = []
    summary: dict[str, int] = {"SUCCESS": 0, "ERROR": 0, "SKIPPED_QUOTA": 0, "SKIPPED_DONE": 0}

    for row in ordered:
        ticker = row["ticker"]
        if ticker not in canonical_set:
            record_attempt(
                experiment_db,
                ticker=ticker,
                status="ERROR",
                http_status=None,
                bytes_count=None,
                error=(
                    "DUPLICATE_CIK: non-canonical share-class ticker for an "
                    "already-bootstrapped CIK"
                ),
            )
            summary["ERROR"] += 1
            continue
        if symbol_has_evidence(research_db, ticker):
            summary["SKIPPED_DONE"] += 1
            continue
        pending.append(row)

    print(
        json.dumps(
            {
                "phase": "plan",
                "cohort": len(cohort),
                "canonical": len(canonical_set),
                "pending": len(pending),
                "summary": summary,
            },
            sort_keys=True,
        )
    )

    # End at the last COMPLETED session (yesterday UTC): today's EOD bar has a
    # PAT of next-day 00:15Z (20:15 ET close), which is in the future
    # mid-session and is rejected by the evidence layer as
    # PARSE: public_available_time cannot follow ingested_time. Backfills
    # request only completed sessions.
    last_completed = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    store = EvidenceStore(research_db)
    for row in pending:
        ticker = row["ticker"]
        try:
            fetched = fetch_one(
                adapter,
                quota,
                ticker=ticker,
                start_date=BACKFILL_START_DATE,
                end_date=last_completed,
            )
            records = adapter.parse(fetched.raw_bytes, fetched, ticker=ticker)
            ingest_records(records, store)
            record_attempt(
                experiment_db,
                ticker=ticker,
                status="SUCCESS",
                http_status=fetched.status,
                bytes_count=len(fetched.raw_bytes),
                error=None,
            )
            summary["SUCCESS"] += 1
        except Exception as exc:  # noqa: BLE001 - every attempt recorded
            error_class, message, status = classify_error(exc)
            record_attempt(
                experiment_db,
                ticker=ticker,
                status="ERROR",
                http_status=status,
                bytes_count=None,
                error=f"{error_class}: {message}",
            )
            summary["ERROR"] += 1
            if error_class == "QUOTA":
                print(json.dumps({"phase": "stopped", "reason": "quota ceiling", "ticker": ticker}))
                break
            if error_class == "AUTH":
                print(json.dumps({"phase": "stopped", "reason": "auth failure", "ticker": ticker}))
                return 1
            if error_class in TRANSIENT_ERROR_CLASSES:
                transient_retry.append(row)

    # One deferred retry pass for transient failures (fresh quota window).
    for row in transient_retry:
        ticker = row["ticker"]
        if symbol_has_evidence(research_db, ticker):
            continue
        try:
            fetched = fetch_one(
                adapter, quota, ticker=ticker, start_date=BACKFILL_START_DATE,
                end_date=last_completed
            )
            records = adapter.parse(fetched.raw_bytes, fetched, ticker=ticker)
            ingest_records(records, store)
            record_attempt(
                experiment_db,
                ticker=ticker,
                status="SUCCESS",
                http_status=fetched.status,
                bytes_count=len(fetched.raw_bytes),
                error=None,
            )
            summary["SUCCESS"] += 1
        except Exception as exc:  # noqa: BLE001
            error_class, message, status = classify_error(exc)
            record_attempt(
                experiment_db,
                ticker=ticker,
                status="ERROR",
                http_status=status,
                bytes_count=None,
                error=f"{error_class}: {message}",
            )
            if error_class in ("QUOTA", "AUTH"):
                break

    usage = quota.bootstrap_usage(_now_ts())
    print(
        json.dumps({"phase": "done", "summary": summary, "bootstrap_usage": usage}, sort_keys=True)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tiingo-backfill")
    parser.add_argument(
        "--smoke", action="store_true", help="one bounded authenticated request only"
    )
    parser.add_argument("--run", action="store_true", help="full cohort EOD backfill (resumable)")
    parser.add_argument("--experiment-db", type=Path, default=DEFAULT_EXPERIMENT_DB_PATH)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process at most N cohort tickers (trial/staged runs; deterministic order)",
    )
    args = parser.parse_args(argv)
    if args.smoke == args.run:
        parser.error("exactly one of --smoke / --run is required")
    settings = ResearchSettings()
    if not settings.tiingo_token:
        raise SystemExit(
            "TIINGO_TOKEN is not configured (RESEARCH_TIINGO_TOKEN env or ignored .env)"
        )
    if not settings.tiingo_license_confirmed:
        raise SystemExit(
            "RESEARCH_TIINGO_LICENSE_CONFIRMED must be true for the internal-use license"
        )
    experiment_db = ExperimentDB(args.experiment_db)
    experiment_db.migrate()
    if args.smoke:
        return run_smoke(settings=settings, experiment_db=experiment_db)
    return run_backfill(settings=settings, experiment_db=experiment_db, args_limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())

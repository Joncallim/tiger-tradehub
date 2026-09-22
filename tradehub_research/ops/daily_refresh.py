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
from tradehub_research.ops import refresh_runs
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
#: Rotation priority for a symbol that holds no bars at all. Larger than any
#: reachable sessions-behind value, so "no data" is served ahead of "old data"
#: rather than behind it -- `_sessions_behind(None, ...)` returns -1, and using
#: that raw sentinel as a rank is what would push it to the back of the queue.
NEVER_INGESTED_RANK = 1 << 30
#: At most this fraction of a run's rotation budget may be spent on symbols
#: whose most recent attempt failed. Without a ceiling, a cohort of persistently
#: failing symbols as large as the budget consumes the whole rotation every run
#: (a failure never advances a bar, so it returns at the front, forever) and
#: healthy stale names behind it are never attempted.
COOLING_BUDGET_DIVISOR = 4
#: How far back the ledger is read to decide whether a symbol is still failing.
#: Older failures have decayed: the symbol is treated as healthy again.
FAILURE_LOOKBACK_DAYS = 30
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


def rotation_candidates(
    research_db: ResearchDB,
    by_ticker: dict[str, str],
    *,
    as_of: date,
    window_sessions: int,
    retired: set[str] | None = None,
) -> tuple[list[str], int]:
    """Symbols that need a refresh this run, MOST STALE FIRST.

    Returns ``(ordered_tickers, skipped_fresh)``.

    The ordering is part of the contract, not a nicety. The rotation has a hard
    budget and stops the moment it is spent, so a purely alphabetical walk spends
    the whole budget on whatever sorts first and starves the end of the alphabet
    permanently -- the 2026-09-21 state: 144 candidates against a 74-request
    budget, the run refreshed NPHC..SNROF, and the 70 symbols sorting *after*
    SNROF were never fetched. They had last-bar 2026-09-09 and stayed there run
    after run while shorter-stale names kept being served.

    Ranking by sessions-behind descending means the budget is always spent on the
    worst data first. That is the difference between a bias and a bug: an
    alphabetical walk serves the same head-of-alphabet names first on every run,
    so while demand exceeds the budget the tail is deferred again and again --
    which is exactly the 2026-09-09 cohort, still stale after ten days of nightly
    refreshes. Worst-first rotates through the backlog instead, so a shortfall
    shows up as a lag that grows no faster than the budget dictates, rather than
    as a fixed set of names that is never served. Ticker is the tie-break, so the
    walk stays deterministic and re-runs pick the same names.

    A symbol with no bars at all sorts *first*, not last: `_sessions_behind`
    reports "no bars" as the sentinel `-1`, and ranking on that raw value would
    turn the sentinel into a permanent queue-priority penalty for precisely the
    worst data state there is.
    """
    retired = retired or set()
    stale: list[tuple[int, str]] = []
    skipped_fresh = 0
    for ticker in sorted(by_ticker):
        if ticker.upper() in retired:
            continue  # delisted/unresolvable -- no longer fetched
        last = _last_bar_date(research_db, by_ticker[ticker])
        behind = _sessions_behind(last, as_of)
        if last is not None and behind <= window_sessions:
            skipped_fresh += 1
            continue
        if symbol_has_evidence(research_db, ticker) is False:
            continue  # never resolvable (UNKNOWN_SYMBOL) -- leave for the ledger
        # No bars at all (CHECKPOINT_LOST) is the worst data state there is, so
        # it ranks above every symbol that merely holds an old bar.
        stale.append((NEVER_INGESTED_RANK if last is None else behind, ticker))
    stale.sort(key=lambda entry: (-entry[0], entry[1]))
    return [ticker for _behind, ticker in stale], skipped_fresh


def _is_quota_block(row) -> bool:
    """Is this ledger row a provider *budget* refusal, not a symbol fault?

    A quota block says nothing about the symbol, so it must never be counted as
    one of its failures -- that is how a budget limit turns into a false
    "this symbol is broken" story.
    """
    if str(row["status"] or "").upper() == "SKIPPED_QUOTA":
        return True
    return str(row["error"] or "").upper().startswith("QUOTA")


def attempt_failure_state(experiment_db, tickers) -> dict[str, dict]:
    """Durable per-symbol failure state, read from the append-only ledger.

    Returns ``{TICKER: {"streak": int, "last_attempt_at": str | None}}``. ``streak``
    is the number of consecutive most-recent attempts that failed; a landing
    fetch resets it to zero, and quota blocks are transparent (neither a failure
    nor a reset). Only the last ``FAILURE_LOOKBACK_DAYS`` are considered, so an
    old failure decays instead of damning a symbol forever.

    The ledger is the authority the diagnosis already classifies from, so the
    rotation and the audit cannot disagree about what happened to a symbol: this
    reads existing retry state rather than standing up a second retry system.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    wanted = {str(t).upper() for t in tickers}
    state: dict[str, dict] = {ticker: {"streak": 0, "last_attempt_at": None} for ticker in wanted}
    if not state or experiment_db is None:
        return state
    since = (datetime.now(timezone.utc) - timedelta(days=FAILURE_LOOKBACK_DAYS)).isoformat()
    try:
        with experiment_db.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT symbol_or_cik, status, error, requested_at FROM backfill_attempt "
                "WHERE requested_at >= ? ORDER BY requested_at DESC",
                (since,),
            ).fetchall()
    except sqlite3.Error:  # ledger unavailable / older schema: treat all as healthy
        return state

    settled: set[str] = set()
    for row in rows:
        ticker = str(row["symbol_or_cik"]).upper()
        if ticker not in state or ticker in settled:
            continue
        entry = state[ticker]
        if entry["last_attempt_at"] is None:
            # Newest attempt of ANY kind: the fairness slice needs recency, and a
            # quota block still tells us the symbol was attempted most recently.
            entry["last_attempt_at"] = row["requested_at"]
        if _is_quota_block(row):
            continue
        if str(row["status"]).upper() == "SUCCESS":
            settled.add(ticker)  # the streak ends here
            continue
        entry["streak"] += 1
    return state


def allocate_rotation(
    order: list[str], *, budget: int, failures: dict[str, dict] | None = None
) -> tuple[list[str], dict[str, str]]:
    """Split an ordered candidate list into ``(to_attempt, deferrals)``.

    ``order`` is worst-stale-first (see :func:`rotation_candidates`). Symbols
    whose recent attempts failed are held back into a **cooling** pool:

    * ready candidates claim the budget first, so a failing symbol can never take
      a slot from one that might actually heal -- but a bounded share is *reserved*
      for cooling retries, because with ready demand permanently at capacity the
      cooling pool would otherwise never be reached at all, and a symbol that
      merely failed once would stay deferred (and, since scheduled work is not
      remediated, quarantined) forever. The reservation never takes the last slot
      from ready work: with a one-request budget the healthy candidate still goes
      first;
    * cooling candidates use the reserved share, least-recently-attempted first, so
      failures are still retried (transient ones recover) and rotate instead of
      monopolising;
    * the share is bounded, so a run never spends its whole budget re-fetching
      symbols already known to be failing.

    Deferrals carry a reason -- ``BUDGET_EXHAUSTED`` (ready, budget ran out) or
    ``COOLING_SLICE`` (withheld by fairness) -- so the diagnosis and the operator
    can tell deliberate scheduling from an interruption.
    """
    budget = max(0, int(budget))
    state = {str(k).upper(): (v or {}) for k, v in (failures or {}).items()}

    def streak_of(ticker: str) -> int:
        return int(state.get(ticker.upper(), {}).get("streak", 0) or 0)

    cooling = {ticker for ticker in order if streak_of(ticker) > 0}
    ready = [ticker for ticker in order if ticker not in cooling]
    cooling_order = sorted(
        cooling,
        key=lambda t: (str(state.get(t.upper(), {}).get("last_attempt_at") or ""), t),
    )

    allowance = max(1, budget // COOLING_BUDGET_DIVISOR) if budget else 0
    # Reserve cooling's retry share. The reservation is skipped entirely when
    # nothing is cooling (the budget belongs to real work), and it never takes the
    # last slot from ready candidates -- with a one-request budget the healthy
    # candidate still goes first. With no ready candidates, cooling may use its
    # whole allowance: there is nothing else worth spending on.
    if not cooling_order:
        reserved = 0
    elif ready:
        # Capped by the retries that actually exist: reserving more than there are
        # cooling symbols would idle budget while stale ready names wait (budget 74
        # with one cooling symbol must not cost 17 ready fetches).
        reserved = min(allowance, len(cooling_order), max(0, budget - 1))
    else:
        reserved = allowance
    chosen: set[str] = set(ready[: budget - reserved])
    cooling_used = 0
    for ticker in cooling_order:
        if len(chosen) >= budget or cooling_used >= reserved:
            break
        chosen.add(ticker)
        cooling_used += 1

    to_attempt = [ticker for ticker in order if ticker in chosen]
    deferrals = {
        ticker: (
            refresh_runs.DEFERRED_COOLING if ticker in cooling else refresh_runs.DEFERRED_BUDGET
        )
        for ticker in order
        if ticker not in chosen
    }
    return to_attempt, deferrals


def _open_refresh_run(paths, run_key: str, *, universe: int, summary: dict):
    """Start (or reset) the durable run record. FAILS CLOSED.

    Returns ``(store, token)``: the token is what ties every later write to *this*
    invocation, so an overlapping same-session run cannot have its reset undone by
    an earlier run's finish.

    The record is what lets the diagnosis tell a deliberate deferral from an
    interruption. Proceeding without it on a same-session re-run would leave a
    previous COMPLETED record and its deferral list in place, and those obsolete
    deferrals would suppress remediation for a session whose latest attempt
    actually failed. So an unwritable record aborts the run *before* any provider
    work rather than fetching under a stale decision.
    """
    store = refresh_runs.store_for(paths)
    token = store.open_run(
        run_key,
        run_key,
        universe=universe,
        window_sessions=summary.get("window_sessions"),
        rotation_budget=summary.get("rotation_budget"),
        candidates=summary.get("rotation_candidates"),
    )
    return store, token


def _update_refresh_run(
    store,
    run_key: str,
    token: str | None,
    *,
    window_sessions: int | None,
    rotation_budget: int | None,
    candidates: int | None,
    summary: dict,
) -> None:
    """Record the facts that are only known after the universe has been read."""
    if store is None or token is None:
        return
    try:
        if not store.update_metadata(
            run_key,
            token=token,
            window_sessions=window_sessions,
            rotation_budget=rotation_budget,
            candidates=candidates,
        ):
            summary["refresh_run_superseded"] = True
    except Exception as exc:  # noqa: BLE001 -- evidence, never the fetch path
        summary["refresh_run_record_error"] = f"{type(exc).__name__}: {exc}"


def _close_refresh_run(
    store,
    run_key: str,
    token: str | None,
    outcomes: dict[str, str],
    summary: dict,
    active_attempted: set[str],
) -> None:
    """Close the run record, or leave it OPEN so it reads as interrupted.

    Only ``OK`` and ``QUOTA_EXHAUSTED`` are closed. A run that died mid-flight
    stays ``RUNNING``; that is what stops a crashed refresh from being read as a
    deliberate deferral.

    The rotation totals are reported separately from the symbol-level outcomes:
    an active-set success is real work, but it is not a rotation candidate served,
    and counting it would make the report quote more work than the rotation
    budget allows on a demand the rotation never had.
    """
    status = {
        "OK": refresh_runs.COMPLETED,
        "QUOTA_EXHAUSTED": refresh_runs.QUOTA_EXHAUSTED,
    }.get(summary.get("status"))
    if store is None or token is None or status is None:
        return
    rotation = {
        ticker: disposition
        for ticker, disposition in outcomes.items()
        if ticker not in active_attempted
    }
    # SUCCESSES, not attempts: `summary["rotation_refreshed"]` counts requests the
    # rotation spent (a failed or empty fetch still spends one), while the report's
    # "served" figure must mean the candidate actually advanced.
    rotation_refreshed = sum(
        1 for disposition in rotation.values() if disposition == refresh_runs.REFRESHED
    )
    rotation_deferred = sum(
        1 for disposition in rotation.values() if disposition in refresh_runs.DEFERRED
    )
    try:
        if not store.finish(
            run_key,
            status,
            outcomes,
            token=token,
            candidates=summary.get("rotation_candidates", len(rotation)),
            refreshed=rotation_refreshed,
            deferred=rotation_deferred,
        ):
            # A newer same-session invocation owns the row now; this run's
            # outcome must not become the session's last word.
            summary["refresh_run_superseded"] = True
            return
    except Exception as exc:  # noqa: BLE001 -- evidence, never the fetch path
        summary["refresh_run_record_error"] = f"{type(exc).__name__}: {exc}"
        return
    summary["refresh_run_status"] = status
    summary["refresh_run_deferred"] = rotation_deferred


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
    # Declared before the try so every exit path can close the run record.
    run_key = as_of.isoformat()
    outcomes: dict[str, str] = {}
    #: Symbols attempted by the active phase: they are real work, but not
    #: rotation candidates, so they are excluded from the rotation totals.
    active_attempted: set[str] = set()
    # Open (or reset) the session record BEFORE the first fallible request. A
    # same-session re-run must invalidate the previous COMPLETED record straight
    # away: if this run then exhausts quota or dies, the old run's deferral list
    # must not survive as authoritative, or the diagnosis would suppress
    # remediation for a session whose latest attempt was interrupted.
    roster, roster_token = _open_refresh_run(
        paths, run_key, universe=len(by_ticker), summary=summary
    )
    try:
        # 1. Active set first: securities with recent production screens.
        active = _active_securities(research_db) & set(by_ticker)
        for ticker in sorted(active)[:active_max]:
            sid = by_ticker[ticker]
            if not _needs_fresh(research_db, sid, as_of.isoformat()):
                summary["SKIPPED_FRESH"] += 1
                continue
            before_success = summary["SUCCESS"]
            _refresh_one(adapter, quota, research_db, experiment_db, store, ticker, as_of, summary)
            summary["active_refreshed"] += 1
            active_attempted.add(ticker)
            # These symbols were genuinely attempted; record the outcome as such
            # rather than letting them read as "deliberately skipped" later. A
            # symbol the active phase failed is cooling in the rotation, so
            # without this it would be recorded DEFERRED_COOLING -- and the
            # diagnosis gives a deferral precedence over the failure, silencing
            # remediation for a real error.
            outcomes[ticker] = (
                refresh_runs.REFRESHED
                if summary["SUCCESS"] > before_success
                else refresh_runs.FAILED
            )
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
        # the capacity plan below sees the true demand, and ordered worst-first
        # so that when the demand exceeds the budget the requests go to the most
        # stale names rather than to the start of the alphabet.
        candidates, skipped_fresh = rotation_candidates(
            research_db,
            by_ticker,
            as_of=as_of,
            window_sessions=window_sessions,
            retired=retired,
        )
        summary["SKIPPED_FRESH"] += skipped_fresh
        # The served/deferred split, and the demand the budget is measured against,
        # are computed below -- over the candidates the symbol ceiling admitted and
        # the active phase did not already attempt.

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

        # Fairness before spending: a symbol whose recent attempts keep failing
        # must not be able to consume the budget that names behind it need. See
        # `allocate_rotation`.
        #
        # Allocate over the ADMITTED candidates only. A candidate the rolling-month
        # ceiling refused cannot be fetched, so letting it hold a budget slot would
        # waste a request the run is allowed to spend and leave later admitted
        # names marked deferred -- a completed run spending less than its budget
        # while refreshable stale names wait.
        # Symbols the active phase already attempted are not fetched again in the
        # same run: one attempt each, and their outcome is already recorded. They
        # are also not rotation DEMAND -- the rotation never had them to serve, and
        # an active FAILURE stays stale, so counting it here would overstate the
        # demand the budget was measured against.
        rotation_demand = [ticker for ticker in candidates if ticker not in active_attempted]
        summary["rotation_candidates"] = len(rotation_demand)
        admitted_candidates = [ticker for ticker in rotation_demand if ticker.upper() in admitted]
        failures = attempt_failure_state(experiment_db, admitted_candidates)
        summary["rotation_cooling"] = sum(
            1
            for ticker in admitted_candidates
            if int((failures.get(ticker.upper()) or {}).get("streak", 0) or 0) > 0
        )
        to_attempt, deferrals = allocate_rotation(
            admitted_candidates, budget=rotation_budget, failures=failures
        )
        summary["rotation_deferred_to_next_run"] = max(
            0, len(admitted_candidates) - rotation_budget
        )

        for ticker in candidates:
            if ticker in outcomes:
                continue  # attempted in the active phase; that outcome stands
            if ticker.upper() not in admitted:
                outcomes[ticker] = refresh_runs.DEFERRED_CAPACITY
            elif ticker in deferrals:
                outcomes[ticker] = deferrals[ticker]
        _update_refresh_run(
            roster,
            run_key,
            roster_token,
            window_sessions=window_sessions,
            rotation_budget=rotation_budget,
            candidates=summary["rotation_candidates"],
            summary=summary,
        )

        for ticker in to_attempt:
            before_success = summary["SUCCESS"]
            _refresh_one(adapter, quota, research_db, experiment_db, store, ticker, as_of, summary)
            outcomes[ticker] = (
                refresh_runs.REFRESHED
                if summary["SUCCESS"] > before_success
                else refresh_runs.FAILED
            )
            summary["rotation_refreshed"] += 1
            rotated += 1
    except RuntimeError as exc:
        if "quota" in str(exc):
            summary["status"] = "QUOTA_EXHAUSTED"
            _close_refresh_run(roster, run_key, roster_token, outcomes, summary, active_attempted)
            return summary
        raise  # an unexpected fault leaves the run OPEN: reads as interrupted
    summary["status"] = "OK"
    _close_refresh_run(roster, run_key, roster_token, outcomes, summary, active_attempted)
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

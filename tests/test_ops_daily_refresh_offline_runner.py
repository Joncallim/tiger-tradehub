"""Offline runner regression harness for the daily-refresh active-set seam.

The harness uses the production runner, real research/validation migrations and
the evidence store.  The sole provider seam is a module-level fake adapter: it
does not have a transport, token, cache, or network capability.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradehub_research.adapters.base import Envelope, FetchResult, ParsedRecord
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import refresh_runs
from tradehub_research.validation.experiment_db import ExperimentDB

AS_OF = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def _isolate_retirement_state(tmp_path, monkeypatch):
    """Never let an offline runner test read the host retirement ledger."""
    monkeypatch.setattr(dr, "RETIRED_FILE", tmp_path / "retired_securities.json")


class FakeQuota:
    """Read-only capacity view for the runner's real capacity planner."""

    def __init__(self, *, used: int = 0, symbols: list[str] | None = None) -> None:
        self.used = used
        self.symbols = symbols or []

    def bootstrap_usage(self, _now: float, limit: int = 450) -> dict:
        return {
            "used": self.used,
            "remaining": max(0, limit - self.used),
            "limit": limit,
            "symbols": [{"symbol": symbol} for symbol in self.symbols],
        }


class FakeTiingoAdapter:
    """Deterministic provider replacement; every call is observable in ``fetches``."""

    def __init__(self, quota: FakeQuota, *, crash_on: set[str] | None = None) -> None:
        self.quota = quota
        self.crash_on = crash_on or set()
        self.fetches: list[str] = []

    def fetch_prices(self, ticker: str, start_date: str, end_date: str) -> FetchResult:
        del start_date, end_date
        self.fetches.append(ticker)
        if ticker in self.crash_on:
            # ``_refresh_one`` records ordinary provider errors.  A process
            # interruption is deliberately outside ``Exception`` so the runner
            # leaves its durable record RUNNING for a conservative rerun.
            raise KeyboardInterrupt(f"injected offline interruption for {ticker}")
        payload = json.dumps(
            [
                {
                    "date": f"{AS_OF.isoformat()}T00:00:00.000Z",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 100,
                },
            ]
        ).encode()
        return FetchResult(
            url=f"offline://tiingo/{ticker}",
            retrieved_at="2026-10-24T00:30:00Z",
            status=200,
            headers={},
            raw_bytes=payload,
            cache_path=Path("offline-fake"),
        )

    def parse(self, raw: bytes, fetched: FetchResult, *, ticker: str) -> list[ParsedRecord]:
        # Keep parsing in the fake intentionally tiny; ingestion below remains
        # production code and writes append-only evidence through EvidenceStore.
        del raw
        return [
            ParsedRecord(
                Envelope(
                    source_id="tiingo_eod",
                    source_url=fetched.url,
                    source_record_id=f"{ticker}:{AS_OF}:offline-bar",
                    retrieved_at=fetched.retrieved_at,
                    http_status=fetched.status,
                    etag=None,
                    last_modified=None,
                    raw_content_hash=fetched.raw_content_hash,
                    parser_version="offline-runner-regression",
                    event_time=f"{AS_OF}T00:00:00Z",
                    public_available_time="2026-10-24T00:15:00Z",
                    pat_provenance="derived_from_index",
                ),
                ticker,
                "ticker",
                {
                    "record_type": "price_bar",
                    "provider_ticker": ticker,
                    "session_date": AS_OF.isoformat(),
                    "close": 10.5,
                },
            ),
        ]


def _paths(tmp_path):
    return SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)


def _settings(tmp_path):
    return SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token="offline-test-token-not-used",
        tiingo_license_confirmed=True,
        adapter_cache_dir=tmp_path / "offline-cache",
    )


def _seed(
    paths,
    tickers: list[str],
    *,
    active: set[str] | None = None,
) -> ExperimentDB:
    """Seed real migrated schema, including the production screen join used by active selection."""
    active = active or set()
    database = ResearchDB(paths.research_db, 5000)
    database.migrate()
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "offline runner fixture", "derived_from_index"),
        )
        for ticker in tickers:
            sid = f"security-{ticker}"
            conn.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    ticker,
                    "US",
                    ticker,
                    None,
                    None,
                    "SUPPORTED",
                    "2026-01-01T00:00:00Z",
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO universe_membership "
                "(security_id,price,market_cap,avg_dollar_volume,price_eligible,"
                "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,"
                "knowledge_time,pat_provenance,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    None,
                    None,
                    None,
                    0,
                    0,
                    0,
                    1,
                    "2026-01-01T00:00:00Z",
                    None,
                    "2026-01-01T00:00:00Z",
                    "derived_from_index",
                    None,
                ),
            )
        if active:
            conn.execute(
                "INSERT INTO screen_definition VALUES (?,?,?,?,?,?)",
                (
                    "active-config",
                    "quality",
                    "offline-active",
                    1,
                    "{}",
                    utc_now(),
                ),
            )
            conn.execute(
                "INSERT INTO pipeline_run "
                "(run_id,as_of,universe_hash,screen_manifest_json,screen_manifest_hash,"
                "funnel_config_json,funnel_config_hash,input_snapshot_id,input_view_hash,"
                "expected_security_count,status,failure_json,started_at,finished_at,flags_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "active-run",
                    AS_OF.isoformat(),
                    "universe",
                    "[]",
                    "manifest",
                    "{}",
                    "funnel",
                    None,
                    "view",
                    len(tickers),
                    "RUNNING",
                    None,
                    utc_now(),
                    None,
                    "{}",
                ),
            )
            for ticker in active:
                conn.execute(
                    "INSERT INTO screen_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"active-{ticker}",
                        "active-run",
                        f"security-{ticker}",
                        "active-config",
                        "{}",
                        "[]",
                        "[]",
                        1,
                        1,
                        1.0,
                        1.0,
                        f"result-{ticker}",
                        utc_now(),
                    ),
                )
    store = EvidenceStore(database)
    for ticker in tickers:
        store.insert(
            security_id=f"security-{ticker}",
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "session_date": "2026-10-01",
            },
            extraction_confidence=1.0,
            event_time="2026-10-01T00:00:00Z",
            public_available_time="2026-10-02T00:15:00Z",
            pat_provenance="derived_from_index",
            source_record_id=f"{ticker}:seed-old-bar",
        )
    experiment_db = ExperimentDB(paths.research_dir / "experiment.db")
    experiment_db.migrate()
    return experiment_db


def _run(monkeypatch, tmp_path, tickers, *, active=None, quota=None, **kwargs):
    paths = _paths(tmp_path)
    experiment_db = _seed(paths, tickers, active=active)
    adapter = FakeTiingoAdapter(quota or FakeQuota())
    monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **_kw: adapter)
    summary = dr.run_daily_refresh(
        settings=_settings(tmp_path),
        experiment_db=experiment_db,
        paths=paths,
        as_of=AS_OF,
        **kwargs,
    )
    return summary, adapter, paths, experiment_db


def _evidence_count(paths) -> int:
    with ResearchDB(paths.research_db).connect(read_only=True) as conn:
        return conn.execute("SELECT COUNT(*) FROM evidence_event").fetchone()[0]


def _attempt_count(experiment_db) -> int:
    with experiment_db.connect(read_only=True) as conn:
        return conn.execute("SELECT COUNT(*) FROM backfill_attempt").fetchone()[0]


def test_active_security_ids_are_mapped_back_to_tickers_before_the_real_runner_fetches(
    tmp_path, monkeypatch
):
    """Fails on the source baseline: ``{'security-ACT'} & {'ACT'}`` is empty."""
    summary, adapter, _paths_value, _experiment_db = _run(
        monkeypatch,
        tmp_path,
        ["ACT"],
        active={"ACT"},
        active_max=1,
        rotation_budget=0,
    )
    assert adapter.fetches == ["ACT"]
    assert summary["active_refreshed"] == 1


def test_fresh_active_candidate_is_skipped_without_a_provider_fetch(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    experiment_db = _seed(paths, ["FRESH"], active={"FRESH"})
    EvidenceStore(ResearchDB(paths.research_db)).insert(
        security_id="security-FRESH",
        source_id="tiingo_eod",
        structured_fields={
            "record_type": "price_bar",
            "session_date": AS_OF.isoformat(),
        },
        extraction_confidence=1.0,
        event_time=f"{AS_OF}T00:00:00Z",
        public_available_time="2026-10-24T00:15:00Z",
        pat_provenance="derived_from_index",
        source_record_id="fresh-bar",
    )
    adapter = FakeTiingoAdapter(FakeQuota())
    monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **_kw: adapter)
    summary = dr.run_daily_refresh(
        settings=_settings(tmp_path),
        experiment_db=experiment_db,
        paths=paths,
        as_of=AS_OF,
        rotation_budget=0,
    )
    assert adapter.fetches == []
    assert summary["SKIPPED_FRESH"] >= 1


def test_active_rotation_overlap_is_fetched_once(tmp_path, monkeypatch):
    _summary, adapter, _paths_value, _experiment_db = _run(
        monkeypatch,
        tmp_path,
        ["OVERLAP"],
        active={"OVERLAP"},
        active_max=1,
        rotation_budget=1,
    )
    assert adapter.fetches == ["OVERLAP"]


def test_same_session_rerun_adds_no_duplicate_attempt_or_evidence(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    experiment_db = _seed(paths, ["ONCE"], active={"ONCE"})
    adapter = FakeTiingoAdapter(FakeQuota())
    monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **_kw: adapter)
    kwargs = dict(
        settings=_settings(tmp_path),
        experiment_db=experiment_db,
        paths=paths,
        as_of=AS_OF,
        rotation_budget=0,
    )
    dr.run_daily_refresh(**kwargs)
    dr.run_daily_refresh(**kwargs)
    assert adapter.fetches == ["ONCE"]
    assert _attempt_count(experiment_db) == 1
    assert _evidence_count(paths) == 2  # one seeded stale bar, one fresh runner result


def test_active_cap_leaves_the_unserved_active_name_as_a_durable_deferral(tmp_path, monkeypatch):
    _summary, adapter, paths, _experiment_db = _run(
        monkeypatch,
        tmp_path,
        ["A", "B"],
        active={"A", "B"},
        active_max=1,
        rotation_budget=0,
    )
    assert adapter.fetches == ["A"]
    assert (
        refresh_runs.store_for(paths).outcomes(AS_OF.isoformat())["B"]
        == refresh_runs.DEFERRED_BUDGET
    )


def test_capacity_exhaustion_defers_without_a_provider_fetch(tmp_path, monkeypatch):
    summary, adapter, paths, _experiment_db = _run(
        monkeypatch,
        tmp_path,
        ["CAP"],
        quota=FakeQuota(used=450),
        active_max=0,
        rotation_budget=1,
    )
    assert adapter.fetches == []
    assert summary["symbol_capacity_deferred"] == ["CAP"]
    assert (
        refresh_runs.store_for(paths).outcomes(AS_OF.isoformat())["CAP"]
        == refresh_runs.DEFERRED_CAPACITY
    )


def test_interrupted_same_session_rerun_invalidates_prior_deferrals_and_recovers(
    tmp_path, monkeypatch
):
    _summary, adapter, paths, experiment_db = _run(
        monkeypatch,
        tmp_path,
        ["A", "B"],
        active_max=0,
        rotation_budget=1,
    )
    assert adapter.fetches == ["A"]
    adapter.crash_on.add("B")
    with pytest.raises(KeyboardInterrupt, match="offline interruption"):
        dr.run_daily_refresh(
            settings=_settings(tmp_path),
            experiment_db=experiment_db,
            paths=paths,
            as_of=AS_OF,
            active_max=0,
            rotation_budget=1,
        )
    store = refresh_runs.store_for(paths)
    assert store.run(AS_OF.isoformat())["status"] == refresh_runs.RUNNING
    assert store.completed_deferrals(AS_OF.isoformat()) == {}

    # A later clean invocation owns the same session and offers the stale work
    # again. The old completed deferral cannot poison this recovery run.
    adapter.crash_on.remove("B")
    recovered = dr.run_daily_refresh(
        settings=_settings(tmp_path),
        experiment_db=experiment_db,
        paths=paths,
        as_of=AS_OF,
        active_max=0,
        rotation_budget=1,
    )
    assert adapter.fetches == ["A", "B", "B"]
    assert recovered["rotation_refreshed"] == 1
    assert store.run(AS_OF.isoformat())["status"] == refresh_runs.COMPLETED


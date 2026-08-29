"""Tiingo EOD backfill driver: plan/skip/classification/ledger contracts."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from tradehub_research.backfill.tiingo_driver import (
    classify_error,
    load_cohort_tickers,
    record_attempt,
    symbol_has_evidence,
)
from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import ExperimentDB

SAMPLE = [
    {"cik": "0000000001", "ticker": "AAAA", "title": "Aaa Co"},
    {"cik": "0000000002", "ticker": "AAAB", "title": "Aab Co"},
    {"cik": "0000000002", "ticker": "AAAC", "title": "Aab Pref"},
]


def _seed_dbs(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO universe_sample VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "sample-1",
                "https://www.sec.gov/files/company_tickers.json",
                "h" * 64,
                1,
                "sha256(seed+NUL+ticker) ascending take-450; BOOTSTRAP_COHORT",
                3,
                json.dumps(SAMPLE),
                3,
                "2026-08-27T00:00:00Z",
            ),
        )
    with research_db.connect() as conn:
        for cik, ticker, title in (
            ("0000000001", "AAAA", "Aaa Co"),
            ("0000000002", "AAAB", "Aab Co"),
        ):
            conn.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (cik, ticker, "US", title, None, None, "SUPPORTED", "2026-08-27T00:00:00Z", None),
            )
            conn.execute(
                "INSERT INTO universe_membership "
                "(security_id,price,market_cap,avg_dollar_volume,price_eligible,"
                "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,"
                "knowledge_time,pat_provenance,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cik,
                    None,
                    None,
                    None,
                    0,
                    0,
                    0,
                    1,
                    "2026-08-27T00:00:00Z",
                    None,
                    "2026-08-27T00:00:00Z",
                    "derived_from_index",
                    None,
                ),
            )
    return experiment_db, research_db


def test_load_cohort_tickers_returns_frozen_rows(tmp_path):
    experiment_db, _ = _seed_dbs(tmp_path)
    cohort = load_cohort_tickers(experiment_db)
    assert [row["ticker"] for row in cohort] == ["AAAA", "AAAB", "AAAC"]


def test_canonical_tickers_restricted_to_eligible_members(tmp_path):
    from tradehub_research.backfill.tiingo_driver import canonical_tickers_by_cik

    _, research_db = _seed_dbs(tmp_path)
    canonical = canonical_tickers_by_cik(research_db)
    assert canonical == {"0000000001": "AAAA", "0000000002": "AAAB"}


def test_symbol_has_evidence_after_ingest(tmp_path):
    from tradehub_research.adapters.base import ingest_records
    from tradehub_research.adapters.tiingo import TiingoEodAdapter
    from tradehub_research.evidence import EvidenceStore

    class FakeTransport:
        """HttpTransport protocol: build_request + send over a MockTransport."""

        def __init__(self, handler):
            self._mock = httpx.MockTransport(handler)

        def build_request(self, method, url, **kwargs):
            kwargs.pop("timeout", None)
            return httpx.Request(method, url, **kwargs)

        def send(self, request, *, stream=False):
            response = self._mock.handle_request(request)
            if stream:
                response.read()
            return response

    _, research_db = _seed_dbs(tmp_path)
    adapter = TiingoEodAdapter(
        token="t" * 40,
        license_confirmed=True,
        user_agent="test",
        cache_dir=tmp_path / "cache",
        transport=FakeTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                json=[
                    {
                        "date": "2026-08-25T00:00:00+00:00",
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.0,
                        "close": 10.5,
                        "volume": 1000,
                    }
                ],
            )
        ),
    )
    fetched = adapter.fetch_prices("AAAA", "2026-08-25", "2026-08-25")
    records = adapter.parse(fetched.raw_bytes, fetched, ticker="AAAA")
    ingest_records(records, EvidenceStore(research_db))
    assert symbol_has_evidence(research_db, "AAAA") is True
    assert symbol_has_evidence(research_db, "AAAB") is False


def test_attempt_rows_append_only_and_classified(tmp_path):
    experiment_db, _ = _seed_dbs(tmp_path)
    record_attempt(
        experiment_db, ticker="AAAA", status="SUCCESS", http_status=200, bytes_count=10, error=None
    )
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute("SELECT * FROM backfill_attempt").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "SUCCESS"
    with pytest.raises(sqlite3.Error):
        with experiment_db.connect() as conn:
            conn.execute("UPDATE backfill_attempt SET status='ERROR'")
    with pytest.raises(sqlite3.Error):
        with experiment_db.connect() as conn:
            conn.execute("DELETE FROM backfill_attempt")


def test_classify_error_buckets():
    import httpx as h

    assert (
        classify_error(
            h.HTTPStatusError("x", request=h.Request("GET", "http://x"), response=h.Response(401))
        )[0]
        == "AUTH"
    )
    assert (
        classify_error(
            h.HTTPStatusError("x", request=h.Request("GET", "http://x"), response=h.Response(404))
        )[0]
        == "UNKNOWN_SYMBOL"
    )
    assert (
        classify_error(
            h.HTTPStatusError("x", request=h.Request("GET", "http://x"), response=h.Response(429))
        )[0]
        == "RATE_LIMITED"
    )
    assert (
        classify_error(RuntimeError("Tiingo quota reserve reached; ingestion failed closed"))[0]
        == "QUOTA"
    )
    assert (
        classify_error(ValueError("security identity is unresolved: ticker=AAAC"))[0]
        == "DUPLICATE_CIK"
    )
    assert classify_error(ConnectionError("boom"))[0] == "NETWORK"

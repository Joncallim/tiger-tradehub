from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tradehub_research.adapters.base import FetchResult, NetworkClient, TokenBucket, ingest_records
from tradehub_research.adapters.sec import SecAdapter
from tradehub_research.adapters.tiingo import TiingoEodAdapter, TiingoQuota
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore

FIXTURES = Path(__file__).parent / "fixtures"


def fetched(name: str) -> FetchResult:
    path = FIXTURES / name
    return FetchResult(
        "fixture://" + name, "2025-03-01T00:00:00Z", 200, {}, path.read_bytes(), path
    )


def sec(tmp_path, **kwargs):
    return SecAdapter(
        user_agent="TigerTradeHub tests test@example.com", cache_dir=tmp_path, **kwargs
    )


def test_sec_index_exact_forms_and_conservative_pat(tmp_path):
    rows = sec(tmp_path).parse_daily_index(
        fetched("sec_master.idx").raw_bytes, fetched("sec_master.idx")
    )
    assert [r.structured_fields["form"] for r in rows] == ["4", "4/A"]
    assert rows[0].envelope.public_available_time == "2025-01-04T05:00:00+00:00"
    assert rows[0].envelope.pat_provenance == "derived_from_index"
    assert (
        SecAdapter.revisit_days(__import__("datetime").date(2025, 1, 4))[0].isoformat()
        == "2025-01-03"
    )


def test_sec_companyfacts_curated_accession_aware_and_missing_not_zero(tmp_path):
    meta = fetched("sec_companyfacts.json")
    rows = sec(tmp_path).parse_companyfacts(
        meta.raw_bytes, meta, {"0000001001-25-000003": "2025-02-10T21:02:03Z"}
    )
    assert {r.structured_fields["metric"] for r in rows} == {"revenue", "net_income"}
    assert all(r.envelope.pat_provenance == "source_reported" for r in rows)
    assert all(r.structured_fields["accession"] == "0000001001-25-000003" for r in rows)
    assert "assets" not in {r.structured_fields["metric"] for r in rows}


def test_form4_raw_xml_and_amendment_supersession(tmp_path):
    meta = fetched("sec_form4.xml")
    row = sec(tmp_path).parse_form4(
        meta.raw_bytes,
        meta,
        accession="0000001001-25-000002",
        acceptance_time="2025-01-04T18:30:00Z",
        supersedes_accession="0000001001-25-000001",
    )[0]
    assert row.structured_fields["transaction_code"] == "P"
    assert row.structured_fields["acquired_disposed"] == "A"
    assert row.structured_fields["direct_indirect"] == "D"
    assert row.structured_fields["amendment"] is True
    assert row.envelope.supersedes_source_record_id.endswith(":tx:n:1")


def test_tiingo_license_and_token_fail_closed_without_network(tmp_path):
    class Never:
        def get(self, *_args, **_kwargs):
            raise AssertionError("network called")

    with pytest.raises(ValueError, match="license"):
        TiingoEodAdapter(
            token="secret",
            license_confirmed=False,
            user_agent="ua",
            cache_dir=tmp_path,
            transport=Never(),
        )
    with pytest.raises(ValueError, match="token"):
        TiingoEodAdapter(
            token=None,
            license_confirmed=True,
            user_agent="ua",
            cache_dir=tmp_path,
            transport=Never(),
        )


def test_tiingo_raw_bars_actions_pat_and_adjusted_audit_only(tmp_path):
    adapter = TiingoEodAdapter(
        token="secret", license_confirmed=True, user_agent="ua", cache_dir=tmp_path
    )
    meta = fetched("tiingo_eod.json")
    rows = adapter.parse(meta.raw_bytes, meta, ticker="EXM")
    assert [r.structured_fields["record_type"] for r in rows] == [
        "price_bar",
        "split",
        "dividend",
        "price_bar",
    ]
    assert rows[0].envelope.public_available_time == "2025-01-03T01:15:00+00:00"
    assert rows[0].structured_fields["close"] == 10.5
    assert rows[0].structured_fields["provider_adjusted_audit_only"]["adjClose"] == 5.25
    assert "adjClose" not in {"open", "high", "low", "close", "volume"}


def test_network_timeout_retry_after_and_cache(tmp_path):
    calls, sleeps = [], []

    class Fake:
        def get(self, url, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "2"}, request=httpx.Request("GET", url)
                )
            return httpx.Response(200, content=b"ok", request=httpx.Request("GET", url))

    client = NetworkClient(
        user_agent="descriptive", cache_dir=tmp_path, transport=Fake(), sleep=sleeps.append
    )
    result = client.fetch("https://example.test/data")
    assert result.cache_path.read_bytes() == b"ok" and sleeps == [2.0]
    timeout = calls[0]["timeout"]
    assert timeout.connect == 10 and timeout.read == 30


def test_token_bucket_and_tiingo_reserve():
    clock = [0.0]
    sleeps = []

    def sleep(value):
        sleeps.append(value)
        clock[0] += value

    bucket = TokenBucket(2, clock=lambda: clock[0], sleep=sleep)
    bucket.acquire()
    bucket.acquire()
    assert sleeps == [0.5]
    quota = TiingoQuota(hourly_limit=10, daily_limit=100)
    for _ in range(9):
        quota.acquire(1000)
    with pytest.raises(RuntimeError, match="reserve"):
        quota.acquire(1000)


def test_common_ingest_resolves_identity_and_is_idempotent(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "EXM", "NYSE", "Example", None, None, "SUPPORTED", "2025-01-01", None),
        )
        for source_id in ("tiingo_eod", "sec_index", "sec_xbrl", "sec_form4"):
            db.execute(
                "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
                (source_id, "provider", 1, "adapter", "derived_from_index"),
            )
    adapter = TiingoEodAdapter(
        token="secret", license_confirmed=True, user_agent="ua", cache_dir=tmp_path
    )
    meta = fetched("tiingo_eod.json")
    records = adapter.parse(meta.raw_bytes, meta, ticker="EXM")
    store = EvidenceStore(database)
    first = ingest_records(records, store)
    second = ingest_records(records, store)
    assert first == second and len(store.current("s1")) == 4
    with pytest.raises(ValueError, match="unresolved"):
        ingest_records(adapter.parse(meta.raw_bytes, meta, ticker="MISSING"), store, dry_run=True)

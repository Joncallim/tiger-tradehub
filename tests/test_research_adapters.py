from __future__ import annotations

import json
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
        filed="2025-01-03",
        acceptance_time="2025-01-04T18:30:00Z",
        supersedes_accession="0000001001-25-000001",
    )[0]
    assert row.structured_fields["transaction_code"] == "P"
    assert row.structured_fields["acquired_disposed"] == "A"
    assert row.structured_fields["direct_indirect"] == "D"
    assert row.structured_fields["amendment"] is True
    assert row.envelope.supersedes_source_record_id is None
    assert row.structured_fields["shares"] == 100
    assert row.structured_fields["price_per_share"] == 12.5
    assert row.structured_fields["owner_id"] == "2001"


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

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, content=b"ok")

    client = NetworkClient(
        user_agent="descriptive",
        cache_dir=tmp_path,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    result = client.fetch("https://example.test/data")
    assert result.cache_path.read_bytes() == b"ok" and sleeps == [2.0]
    assert calls[0].extensions["timeout"]["connect"] == 10
    assert calls[0].extensions["timeout"]["read"] == 30


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


def test_form4_parser_to_hunter_can_pass(tmp_path):
    from tradehub_research.hunters import informed_activity
    from tradehub_research.screens import ScreenContext

    meta = fetched("sec_form4.xml")
    parsed = sec(tmp_path).parse_form4(
        meta.raw_bytes,
        meta,
        accession="a",
        filed="2025-01-03",
        acceptance_time="2025-01-04T18:30:00Z",
    )[0]
    one = {
        **parsed.structured_fields,
        "evidence_id": "e1",
        "public_available_time": parsed.envelope.public_available_time,
        "shares": 10000,
        "owner_id": "o1",
    }
    two = {**one, "evidence_id": "e2", "owner_id": "o2"}
    days = informed_activity._window_dates(__import__("datetime").date(2025, 4, 1), 90)
    shares = {
        "metric": "shares_outstanding",
        "concept": "EntityCommonStockSharesOutstanding",
        "value": 1_000_000,
        "unit": "shares",
        "period_end": "2025-03-01",
        "public_available_time": "2025-03-02T00:00:00Z",
        "evidence_id": "shares",
    }
    bar = {
        "session_date": "2025-03-31",
        "close": 10.0,
        "public_available_time": "2025-03-31T23:15:00Z",
        "evidence_id": "bar",
    }
    ctx = ScreenContext(
        facts={"S": [shares]},
        price_bars={"S": [bar]},
        form4={"S": [one, two]},
        identity_events={},
        market_caps={"S": 10_000_000},
        universe=["S"],
        as_of="2025-04-01T01:00:00Z",
        sectors={"S": "Technology"},
        form4_coverage={"S": frozenset(days)},
    )
    assert informed_activity.evaluate(ctx, "S").passed


def test_form4_amendment_reorder_change_remove_and_add_ingests(tmp_path):
    def filing(document_type, transactions):
        rows = "".join(
            f"""
            <nonDerivativeTransaction>
              <securityTitle><value>{title}</value></securityTitle>
              <transactionDate><value>{day}</value></transactionDate>
              <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
              </transactionAmounts>
              <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
            </nonDerivativeTransaction>
            """
            for title, day, shares, price in transactions
        )
        return f"""
        <ownershipDocument>
          <documentType>{document_type}</documentType>
          <periodOfReport>2025-01-04</periodOfReport>
          <issuer><issuerCik>1001</issuerCik><issuerTradingSymbol>EXM</issuerTradingSymbol></issuer>
          <reportingOwner>
            <reportingOwnerId><rptOwnerCik>2001</rptOwnerCik></reportingOwnerId>
            <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>{rows}</nonDerivativeTable>
        </ownershipDocument>
        """.encode()

    database = ResearchDB(tmp_path / "research.db")
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "EXM", "NYSE", "Example", None, None, "SUPPORTED", "2025-01-01", None),
        )
    adapter = sec(tmp_path)
    original_transactions = [
        ("Reordered", "2025-01-01", 10, 1),
        ("Changed", "2025-01-02", 20, 2),
        ("Removed", "2025-01-03", 30, 3),
    ]
    original = adapter.with_security(
        adapter.parse_form4(
            filing("4", original_transactions),
            fetched("sec_form4.xml"),
            accession="original",
            filed="2025-01-04",
        ),
        "s1",
    )
    original_keys = {row.envelope.source_record_id.rsplit(":tx:", 1)[1] for row in original}
    amendment = adapter.with_security(
        adapter.parse_form4(
            filing(
                "4/A",
                [
                    ("Changed", "2025-01-02", 200, 2.5),
                    ("Added", "2025-01-04", 40, 4),
                    ("Reordered", "2025-01-01", 10, 1),
                ],
            ),
            fetched("sec_form4.xml"),
            accession="amendment",
            filed="2025-01-05",
            supersedes_accession="original",
            supersedes_transaction_keys=original_keys,
        ),
        "s1",
    )

    store = EvidenceStore(database)
    ingest_records(original, store)
    ingest_records(amendment, store)

    transaction_rows = [row for row in amendment if not row.envelope.withdrawn]
    assert len(transaction_rows) == 3
    superseding = [
        row for row in transaction_rows if row.envelope.supersedes_source_record_id is not None
    ]
    assert len(superseding) == 2
    added = next(
        row for row in transaction_rows if row.structured_fields["security_title"] == "Added"
    )
    assert added.envelope.supersedes_source_record_id is None
    withdrawals = [row for row in amendment if row.envelope.withdrawn]
    assert len(withdrawals) == 1
    assert withdrawals[0].envelope.supersedes_source_record_id.startswith("original:tx:")
    current = [json.loads(row["structured_fields"]) for row in store.current("s1")]
    assert {row["security_title"] for row in current} == {
        "Reordered",
        "Changed",
        "Added",
    }
    changed = next(row for row in current if row["security_title"] == "Changed")
    assert changed["shares"] == 200
    assert changed["price_per_share"] == 2.5


def test_tiingo_quota_retries_persist_and_bootstrap_boundary(tmp_path):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        status = 500 if len(calls) < 3 else 200
        return httpx.Response(status, content=b"[]")

    adapter = TiingoEodAdapter(
        token="x",
        license_confirmed=True,
        user_agent="ua",
        cache_dir=tmp_path,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _x: None,
    )
    adapter.fetch_prices("AAA", "2025-01-01", "2025-01-02")
    restarted = TiingoQuota(state_path=tmp_path / "tiingo-operational.sqlite")
    assert restarted.remaining(__import__("time").time())["daily"] == 897
    for i in range(1, 450):
        restarted.reserve_bootstrap_symbol(f"S{i:03}", 1000)
    with pytest.raises(RuntimeError, match="450-symbol"):
        restarted.reserve_bootstrap_symbol("OVER", 1000)
    restarted.reserve_bootstrap_symbol("AAA", 1000)  # refresh is free


def test_network_response_byte_ceiling_declared_and_chunked(tmp_path):
    class GuardedStream(httpx.SyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks
            self.consumed = 0
            self.closed = False

        def __iter__(self):
            for chunk in self.chunks:
                self.consumed += 1
                if self.consumed > 3:
                    raise AssertionError("response was consumed beyond the byte ceiling")
                yield chunk

        def close(self):
            self.closed = True

    declared_stream = GuardedStream([b"must not be read"])
    declared_transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, headers={"Content-Length": "9"}, stream=declared_stream
        )
    )
    with pytest.raises(ValueError, match="byte ceiling"):
        NetworkClient(
            user_agent="ua",
            cache_dir=tmp_path,
            transport=httpx.Client(transport=declared_transport),
            max_response_bytes=8,
        ).fetch("https://example.test")
    assert declared_stream.consumed == 0 and declared_stream.closed

    chunked_stream = GuardedStream([b"1234", b"5678", b"9", b"must not be read"])
    chunked_transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, stream=chunked_stream)
    )
    with pytest.raises(ValueError, match="byte ceiling"):
        NetworkClient(
            user_agent="ua",
            cache_dir=tmp_path,
            transport=httpx.Client(transport=chunked_transport),
            max_response_bytes=8,
        ).fetch("https://example.test")
    assert chunked_stream.consumed == 3 and chunked_stream.closed
    assert not [
        p for p in tmp_path.rglob("*") if p.is_file() and p.name != "tiingo-operational.sqlite"
    ]

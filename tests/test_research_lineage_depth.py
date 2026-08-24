from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.universe import SecurityIdentityStore, UniverseMembershipStore

DEPTH = 1_000
TIME_LIMIT_SECONDS = 10


def timestamp(position: int) -> str:
    value = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=position)
    return value.isoformat().replace("+00:00", "Z")


def initialized(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "T0", "NYSE", "Deep", None, None, "SUPPORTED", timestamp(0), None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("src", "filing", 1, "", "source_reported"),
        )
    return database


def timed(call):
    started = perf_counter()
    result = call()
    elapsed = perf_counter() - started
    assert elapsed < TIME_LIMIT_SECONDS
    return result


def test_evidence_deep_lineage_is_linear_and_preserves_terminal_semantics(tmp_path):
    database = initialized(tmp_path)
    midpoint = DEPTH // 2
    with database.connect() as db:
        db.executemany(
            """INSERT INTO evidence_event(
                evidence_id,security_id,source_id,structured_fields,extraction_confidence,
                supersedes_evidence_id,withdrawn,content_hash,source_record_id,event_time,
                public_available_time,pat_provenance,ingested_time
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    f"e{index}",
                    "s1",
                    "src",
                    "{}" if index == midpoint else f'{{"position":{index}}}',
                    1,
                    f"e{index - 1}" if index else None,
                    int(index == midpoint),
                    f"hash-{index}",
                    f"record-{index}",
                    timestamp(index),
                    timestamp(index),
                    "observed_at_ingest" if index % 3 == 1 else "source_reported",
                    timestamp(index),
                )
                for index in range(DEPTH)
            ),
        )
    store = EvidenceStore(database)
    assert [row["evidence_id"] for row in timed(lambda: store.historical(timestamp(0)))] == ["e0"]
    assert timed(lambda: store.historical(timestamp(midpoint))) == []
    assert [row["evidence_id"] for row in timed(lambda: store.historical(timestamp(DEPTH)))] == [
        f"e{DEPTH - 1}"
    ]


def test_universe_deep_lineage_is_linear_at_first_middle_and_last_cutoffs(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.executemany(
            """INSERT INTO universe_membership(
                security_id,price,market_cap,avg_dollar_volume,price_eligible,
                market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,
                knowledge_time,pat_provenance,supersedes_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    "s1",
                    index,
                    None,
                    None,
                    1,
                    1,
                    1,
                    1,
                    timestamp(0),
                    None,
                    timestamp(index),
                    "derived_from_index" if index % 2 else "source_reported",
                    index if index else None,
                )
                for index in range(DEPTH)
            ),
        )
    store = UniverseMembershipStore(database)
    for position in (0, DEPTH // 2, DEPTH - 1):
        rows = timed(lambda position=position: store.pit_valid("s1", timestamp(position)))
        assert [row["price"] for row in rows] == [position]


def test_identity_deep_lineage_is_linear_and_event_time_gated(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.executemany(
            """INSERT INTO security_identity_event(
                security_id,event_type,old_value,new_value,event_time,public_available_time,
                pat_provenance,ingested_time,supersedes_id
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                (
                    "s1",
                    "baseline" if index == 0 else "ticker_change",
                    None if index == 0 else f"T{index - 1}",
                    f"T{index}",
                    timestamp(index),
                    timestamp(index),
                    "source_reported",
                    timestamp(index),
                    index if index else None,
                )
                for index in range(DEPTH)
            ),
        )
    store = SecurityIdentityStore(database)
    for position in (0, DEPTH // 2, DEPTH - 1):
        assert timed(lambda position=position: store.ticker_at("s1", timestamp(position))) == (
            f"T{position}"
        )


def test_ticker_change_requires_publication_and_effective_time(tmp_path):
    database = initialized(tmp_path)
    store = SecurityIdentityStore(database)
    baseline = store.insert(
        security_id="s1",
        event_type="baseline",
        old_value=None,
        new_value="OLD",
        event_time="2025-01-01",
        public_available_time="2025-01-01",
        pat_provenance="source_reported",
    )
    store.insert(
        security_id="s1",
        event_type="ticker_change",
        old_value="OLD",
        new_value="NEW",
        event_time="2025-06-01",
        public_available_time="2025-02-01",
        pat_provenance="source_reported",
        supersedes_id=baseline,
    )
    assert store.ticker_at("s1", "2024-12-31") is None
    assert store.ticker_at("s1", "2025-03-01") == "OLD"
    assert store.ticker_at("s1", "2025-06-01") == "NEW"

from __future__ import annotations

import sqlite3

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore


@pytest.fixture
def store(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "OLD", "NYSE", "Example", "Tech", "Software", "SUPPORTED", "2025-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("filings", "regulatory_filing", 1, "primary", "source_reported"),
        )
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s2", "TWO", "NYSE", "Second", None, None, "SUPPORTED", "2025-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("news", "news", 3, "secondary", "source_reported"),
        )
    return EvidenceStore(database)


def add(store, fields, published, **kwargs):
    return store.insert(
        security_id="s1",
        source_id="filings",
        structured_fields=fields,
        extraction_confidence=0.9,
        event_time=kwargs.pop("event_time", "2025-01-01"),
        public_available_time=published,
        pat_provenance=kwargs.pop("pat_provenance", "source_reported"),
        **kwargs,
    )


def test_publication_not_event_or_ingestion_controls_pit(store):
    item = add(store, {"revenue": 1}, "2025-01-05", ingested_time="2025-01-10")
    assert store.historical("2025-01-04") == []
    assert [row["evidence_id"] for row in store.historical("2025-01-06")] == [item]
    assert [row["evidence_id"] for row in store.historical("2025-01-05")] == [item]


def test_unknown_excluded_historically_but_current(store):
    item = add(store, {"rumor": True}, None, pat_provenance="unknown")
    assert store.historical("2099-01-01") == []
    assert [row["evidence_id"] for row in store.current()] == [item]


def test_null_pat_check_rejects_claimed_historical_provenance(store):
    with pytest.raises(sqlite3.IntegrityError):
        add(store, {"bad": True}, None)


def test_correction_is_point_in_time_and_append_only(store):
    original = add(store, {"revenue": 1}, "2025-01-05")
    correction = add(store, {"revenue": 2}, "2025-01-08", supersedes_evidence_id=original)
    assert [r["evidence_id"] for r in store.historical("2025-01-06")] == [original]
    assert [r["evidence_id"] for r in store.historical("2025-01-09")] == [correction]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.database.connect() as db:
            db.execute(
                "UPDATE evidence_event SET content_hash='changed' WHERE evidence_id=?", (original,)
            )
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_event").fetchone()[0] == 2


def test_duplicate_is_idempotent_per_source_and_security(store):
    first = add(store, {"same": True}, "2025-01-05")
    second = add(store, {"same": True}, "2025-01-05")
    assert second == first
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_event").fetchone()[0] == 1


def test_retraction_removes_live_content_but_retains_rows(store):
    original = add(store, {"claim": True}, "2025-01-05")
    retraction = add(store, {}, "2025-01-08", supersedes_evidence_id=original, withdrawn=True)
    assert [r["evidence_id"] for r in store.historical("2025-01-06")] == [original]
    assert store.historical("2025-01-09") == []
    with store.database.connect(read_only=True) as db:
        assert {r[0] for r in db.execute("SELECT evidence_id FROM evidence_event")} == {
            original,
            retraction,
        }


def test_provenance_histogram(store):
    add(store, {"known": 1}, "2025-01-05")
    add(store, {"unknown": 1}, None, pat_provenance="unknown")
    assert [(r["pat_provenance"], r["count"]) for r in store.provenance_histogram()] == [
        ("source_reported", 1),
        ("unknown", 1),
    ]


def test_source_identity_keeps_equal_period_values_and_withdrawals_distinct(store):
    q1 = add(store, {"revenue": 1}, "2025-01-05", source_record_id="10-Q:2025Q1")
    q2 = add(
        store,
        {"revenue": 1},
        "2025-04-05",
        event_time="2025-04-01",
        source_record_id="10-Q:2025Q2",
    )
    withdrawal_1 = add(
        store, {}, "2025-05-01", event_time="2025-05-01", source_record_id="wd:1", withdrawn=True
    )
    withdrawal_2 = add(
        store, {}, "2025-05-02", event_time="2025-05-02", source_record_id="wd:2", withdrawn=True
    )
    assert len({q1, q2, withdrawal_1, withdrawal_2}) == 4


def test_same_content_different_publication_records_are_distinct(store):
    first = add(store, {"same": 1}, "2025-01-05", source_record_id="publication:1")
    second = add(store, {"same": 1}, "2025-01-06", source_record_id="publication:2")
    assert first != second


def test_identity_retry_rejects_mismatched_metadata(store):
    add(store, {"value": 1}, "2025-01-05", source_record_id="stable-id")
    with pytest.raises(ValueError, match="metadata"):
        add(store, {"value": 2}, "2025-01-05", source_record_id="stable-id")


def test_supersession_scope_cardinality_and_ordering(store):
    original = add(store, {"v": 1}, "2025-01-05", source_record_id="original")
    with pytest.raises(ValueError, match="same security"):
        store.insert(
            security_id="s2",
            source_id="filings",
            structured_fields={"v": 2},
            extraction_confidence=1,
            event_time="2025-01-06",
            public_available_time="2025-01-06",
            pat_provenance="source_reported",
            ingested_time="2025-01-07",
            supersedes_evidence_id=original,
            source_record_id="cross-security",
        )
    with pytest.raises(ValueError, match="same security and source"):
        store.insert(
            security_id="s1",
            source_id="news",
            structured_fields={"v": 2},
            extraction_confidence=1,
            event_time="2025-01-06",
            public_available_time="2025-01-06",
            pat_provenance="source_reported",
            ingested_time="2025-01-07",
            supersedes_evidence_id=original,
            source_record_id="cross-source",
        )
    with pytest.raises(ValueError, match="backdate"):
        add(
            store,
            {"v": 2},
            "2025-01-04",
            ingested_time="2025-01-07",
            supersedes_evidence_id=original,
            source_record_id="backdated",
        )
    successor = add(
        store,
        {"v": 2},
        "2025-01-06",
        ingested_time="2025-01-07",
        supersedes_evidence_id=original,
        source_record_id="successor",
    )
    with pytest.raises(sqlite3.IntegrityError):
        add(
            store,
            {"v": 3},
            "2025-01-07",
            ingested_time="2025-01-08",
            supersedes_evidence_id=original,
            source_record_id="second-successor",
        )
    with pytest.raises(ValueError, match="itself"):
        add(
            store,
            {"v": 4},
            "2025-01-08",
            ingested_time="2025-01-09",
            evidence_id="self",
            supersedes_evidence_id="self",
            source_record_id="self",
        )
    assert store.historical("2025-01-07", "s1")[0]["evidence_id"] == successor


def test_timestamps_normalize_and_pat_cannot_follow_ingestion(store):
    item = add(
        store,
        {"offset": True},
        "2025-01-05T08:00:00+08:00",
        event_time="2025-01-05T01:00:00+01:00",
        ingested_time="2025-01-05T01:00:01Z",
        source_record_id="offset",
    )
    with store.database.connect(read_only=True) as db:
        row = db.execute("SELECT * FROM evidence_event WHERE evidence_id=?", (item,)).fetchone()
    assert row["event_time"] == "2025-01-05T00:00:00Z"
    assert row["public_available_time"] == "2025-01-05T00:00:00Z"
    assert store.historical("2025-01-05T00:00:00+00:00")[0]["evidence_id"] == item
    with pytest.raises(ValueError, match="cannot follow"):
        add(store, {"future": True}, "2025-01-06", ingested_time="2025-01-05")


def test_historical_per_security_query_uses_index(store):
    add(store, {"v": 1}, "2025-01-05")
    as_of = "2025-01-06T00:00:00Z"
    sql = """SELECT e.* FROM evidence_event e
        WHERE e.public_available_time IS NOT NULL
          AND e.pat_provenance IN ('source_reported','derived_from_index')
          AND e.public_available_time <= ? AND e.security_id = ? AND e.withdrawn = 0
          AND NOT EXISTS (SELECT 1 FROM evidence_event successor
            WHERE successor.supersedes_evidence_id=e.evidence_id
              AND successor.public_available_time <= ?)"""
    with store.database.connect(read_only=True) as db:
        rows = db.execute("EXPLAIN QUERY PLAN " + sql, (as_of, "s1", as_of))
        plan = " ".join(row["detail"] for row in rows)
    assert "SEARCH e USING INDEX evidence_pit_idx" in plan
    assert "SCAN e" not in plan

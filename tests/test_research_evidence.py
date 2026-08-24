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
    return EvidenceStore(database)


def add(store, fields, published, **kwargs):
    return store.insert(
        security_id="s1",
        source_id="filings",
        structured_fields=fields,
        extraction_confidence=0.9,
        event_time="2025-01-01",
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

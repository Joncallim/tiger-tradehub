from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import venv

import pytest

import tradehub_research.snapshot as snapshot_module
from tradehub_research.acceptance.runner import run_pack
from tradehub_research.acceptance.schema import Status
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.experiment import ExperimentRegistry
from tradehub_research.schema import PHASE_0_SCHEMA_VERSION
from tradehub_research.snapshot import create_snapshot, open_snapshot_read_only
from tradehub_research.universe import SecurityIdentityStore, UniverseMembershipStore


def initialized(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.init()
    return database


def test_schema_and_migration_are_idempotent(tmp_path):
    database = initialized(tmp_path)
    assert database.migrate() == PHASE_0_SCHEMA_VERSION
    assert database.check()["ok"]
    with database.connect(read_only=True) as db:
        migration_count = db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert migration_count == PHASE_0_SCHEMA_VERSION


def test_settings_use_only_research_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "selected.db"))
    monkeypatch.setenv("TRADEHUB_DB_PATH", "/forbidden")
    monkeypatch.setenv("TIGEROPEN_ID", "secret")
    settings = ResearchSettings()
    assert settings.db_path == tmp_path / "selected.db"
    assert all(
        "tiger" not in name.lower() and "private_key" not in name
        for name in type(settings).model_fields
    )


def test_identity_and_membership_reconstruct_history(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "NEW", "NYSE", "Co", None, None, "LIMITED", "2020-01-01", "2025-04-01"),
        )
        db.execute(
            "INSERT INTO security_identity_event("
            "security_id,event_type,old_value,new_value,event_time,"
            "public_available_time,pat_provenance,ingested_time) VALUES (?,?,?,?,?,?,?,?)",
            (
                "s1",
                "ticker_change",
                "OLD",
                "NEW",
                "2025-02-01",
                "2025-02-01",
                "source_reported",
                "2025-02-01",
            ),
        )
        db.execute(
            "INSERT INTO security_identity_event("
            "security_id,event_type,old_value,new_value,event_time,"
            "public_available_time,pat_provenance,ingested_time) VALUES (?,?,?,?,?,?,?,?)",
            (
                "s1",
                "delisting",
                "listed",
                "delisted",
                "2025-04-01",
                "2025-03-15",
                "source_reported",
                "2025-03-15",
            ),
        )
        db.execute(
            "INSERT INTO universe_membership("
            "security_id,price,market_cap,avg_dollar_volume,price_eligible,"
            "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,"
            "knowledge_time,pat_provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s1",
                20,
                1e9,
                1e7,
                1,
                1,
                1,
                1,
                "2025-01-01",
                "2025-04-01",
                "2025-01-01",
                "source_reported",
            ),
        )
    with database.connect(read_only=True) as db:
        feb = db.execute(
            "SELECT * FROM universe_membership WHERE valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?)",
            ("2025-02-01", "2025-02-01"),
        ).fetchall()
        may = db.execute(
            "SELECT * FROM universe_membership WHERE valid_from <= ? "
            "AND (valid_to IS NULL OR valid_to > ?)",
            ("2025-05-01", "2025-05-01"),
        ).fetchall()
        assert len(feb) == 1 and not may
        assert db.execute("SELECT COUNT(*) FROM security_identity_event").fetchone()[0] == 2


def test_universe_knowledge_gate_corrections_and_identity_events(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "NEW", "NYSE", "Co", None, None, "SUPPORTED", "2020-01-01", None),
        )
    memberships = UniverseMembershipStore(database)
    late = memberships.insert(
        security_id="s1",
        valid_from="2020-01-01",
        knowledge_time="2025-01-01",
        pat_provenance="source_reported",
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
    )
    assert memberships.pit_valid("s1", "2020-06-01") == []
    assert [row["id"] for row in memberships.pit_valid("s1", "2025-01-02")] == [late]
    correction = memberships.insert(
        security_id="s1",
        valid_from="2020-01-01",
        knowledge_time="2025-02-01",
        pat_provenance="source_reported",
        supersedes_id=late,
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=False,
        eligible=False,
    )
    assert [row["id"] for row in memberships.pit_valid("s1", "2025-02-02")] == [correction]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with database.connect() as db:
            db.execute("UPDATE universe_membership SET eligible=1 WHERE id=?", (correction,))

    identities = SecurityIdentityStore(database)
    identities.insert(
        security_id="s1",
        event_type="baseline",
        old_value=None,
        new_value="OLD",
        event_time="2020-01-01",
        public_available_time="2020-01-01",
        pat_provenance="source_reported",
    )
    identities.insert(
        security_id="s1",
        event_type="ticker_change",
        old_value="OLD",
        new_value="NEW",
        event_time="2025-02-01",
        public_available_time="2025-02-02",
        pat_provenance="source_reported",
    )
    assert identities.ticker_at("s1", "2025-01-01") == "OLD"
    assert identities.ticker_at("s1", "2025-03-01") == "NEW"


def test_universe_rejects_backdated_and_forked_corrections(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "ONE", "NYSE", "One", None, None, "SUPPORTED", "2020-01-01", None),
        )
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s2", "TWO", "NYSE", "Two", None, None, "SUPPORTED", "2020-01-01", None),
        )
    memberships = UniverseMembershipStore(database)
    args = dict(
        security_id="s1",
        valid_from="2020-01-01",
        pat_provenance="source_reported",
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
    )
    original = memberships.insert(knowledge_time="2025-02-01", **args)
    with pytest.raises(ValueError, match="cannot backdate"):
        memberships.insert(knowledge_time="2020-01-01", supersedes_id=original, **args)
    correction = memberships.insert(knowledge_time="2025-03-01", supersedes_id=original, **args)
    assert [row["id"] for row in memberships.pit_valid("s1", "2025-02-15")] == [original]
    assert [row["id"] for row in memberships.pit_valid("s1", "2025-03-01")] == [correction]
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        memberships.insert(knowledge_time="2025-04-01", supersedes_id=original, **args)
    with pytest.raises(ValueError, match="same security"):
        memberships.insert(
            **{**args, "security_id": "s2"},
            knowledge_time="2025-04-01",
            supersedes_id=correction,
        )
    with database.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="cannot backdate"):
            db.execute(
                """INSERT INTO universe_membership(
                security_id,price_eligible,market_cap_eligible,liquidity_eligible,eligible,
                valid_from,knowledge_time,pat_provenance,supersedes_id
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                ("s1", 1, 1, 1, 1, "2020-01-01", "2024-01-01", "source_reported", correction),
            )


def test_universe_historical_provenance_gate(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "ONE", "NYSE", "One", None, None, "SUPPORTED", "2020-01-01", None),
        )
    memberships = UniverseMembershipStore(database)
    membership = memberships.insert(
        security_id="s1",
        valid_from="2020-01-01",
        knowledge_time="2020-01-01",
        pat_provenance="unknown",
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
    )
    assert memberships.pit_valid("s1", "2020-01-02") == []
    assert [row["id"] for row in memberships.current("s1")] == [membership]


def test_universe_full_chain_terminal_semantics(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "ONE", "NYSE", "One", None, None, "SUPPORTED", "2020-01-01", None),
        )
    memberships = UniverseMembershipStore(database)
    args = dict(
        security_id="s1",
        valid_from="2020-01-01",
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
    )
    chain = [
        memberships.insert(knowledge_time="2025-01-01", pat_provenance="source_reported", **args)
    ]
    for day, provenance in (
        (2, "observed_at_ingest"),
        (3, "source_reported"),
        (4, "derived_from_index"),
    ):
        chain.append(
            memberships.insert(
                knowledge_time=f"2025-01-0{day}",
                pat_provenance=provenance,
                supersedes_id=chain[-1],
                **args,
            )
        )
    assert [row["id"] for row in memberships.pit_valid("s1", "2025-01-04")] == [chain[-1]]

    observed = memberships.insert(
        knowledge_time="2025-02-01", pat_provenance="source_reported", **args
    )
    memberships.insert(
        knowledge_time="2025-02-02",
        pat_provenance="observed_at_ingest",
        supersedes_id=observed,
        **args,
    )
    assert observed not in [row["id"] for row in memberships.pit_valid("s1", "2025-02-03")]


def test_identity_is_pit_safe_append_only_and_correctable(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "CURRENT", "NYSE", "One", None, None, "SUPPORTED", "2020-01-01", None),
        )
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s2", "TWO", "NYSE", "Two", None, None, "SUPPORTED", "2020-01-01", None),
        )
    identities = SecurityIdentityStore(database)
    assert identities.ticker_at("s1", "2019-01-01") is None
    baseline = identities.insert(
        security_id="s1",
        event_type="baseline",
        old_value=None,
        new_value="OLD",
        event_time="2020-01-01",
        public_available_time="2020-01-02",
        pat_provenance="source_reported",
    )
    assert identities.ticker_at("s1", "2020-01-01") is None
    observed = identities.insert(
        security_id="s1",
        event_type="ticker_change",
        old_value="OLD",
        new_value="POISON",
        event_time="2025-01-01",
        public_available_time=None,
        pat_provenance="observed_at_ingest",
        ingested_time="2025-01-02",
    )
    assert identities.ticker_at("s1", "2025-01-03") == "OLD"
    corrected = identities.insert(
        security_id="s1",
        event_type="ticker_change",
        old_value="OLD",
        new_value="NEW",
        event_time="2025-01-01",
        public_available_time="2025-01-03",
        pat_provenance="source_reported",
        supersedes_id=observed,
    )
    assert identities.ticker_at("s1", "2025-01-02") == "OLD"
    assert identities.ticker_at("s1", "2025-01-03") == "NEW"
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        identities.insert(
            security_id="s1",
            event_type="ticker_change",
            old_value="OLD",
            new_value="FORK",
            event_time="2025-01-01",
            public_available_time="2025-01-04",
            pat_provenance="source_reported",
            supersedes_id=observed,
        )
    with database.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE security_identity_event SET new_value='X' WHERE id=?", (baseline,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM security_identity_event WHERE id=?", (baseline,))
    with pytest.raises(ValueError, match="backdate"):
        identities.insert(
            security_id="s1",
            event_type="ticker_change",
            old_value="OLD",
            new_value="X",
            event_time="2025-01-01",
            public_available_time="2025-01-02",
            pat_provenance="source_reported",
            supersedes_id=corrected,
        )
    with pytest.raises(ValueError, match="same security"):
        identities.insert(
            security_id="s2",
            event_type="ticker_change",
            old_value="TWO",
            new_value="X",
            event_time="2025-01-01",
            public_available_time="2025-01-04",
            pat_provenance="source_reported",
            supersedes_id=corrected,
        )


def test_identity_supersession_domains_do_not_erase_ticker_history(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "OLD", "NYSE", "One", None, None, "SUPPORTED", "2020-01-01", None),
        )
    identities = SecurityIdentityStore(database)
    baseline = identities.insert(
        security_id="s1",
        event_type="baseline",
        old_value=None,
        new_value="OLD",
        event_time="2020-01-01",
        public_available_time="2020-01-01",
        pat_provenance="source_reported",
    )
    for event_type in ("share_class_change", "delisting"):
        with pytest.raises(ValueError, match="compatible event domain"):
            identities.insert(
                security_id="s1",
                event_type=event_type,
                old_value="OLD",
                new_value=None,
                event_time="2025-01-01",
                public_available_time="2025-01-01",
                pat_provenance="source_reported",
                supersedes_id=baseline,
            )
        assert identities.ticker_at("s1", "2026-01-01") == "OLD"

    with database.connect() as db:
        for event_type in ("share_class_change", "delisting"):
            with pytest.raises(sqlite3.IntegrityError, match="compatible event domain"):
                db.execute(
                    """INSERT INTO security_identity_event(
                    security_id,event_type,old_value,new_value,event_time,
                    public_available_time,pat_provenance,ingested_time,supersedes_id
                    ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        "s1",
                        event_type,
                        "OLD",
                        None,
                        "2025-01-01",
                        "2025-01-01",
                        "source_reported",
                        "2025-01-01",
                        baseline,
                    ),
                )

    ticker_change = identities.insert(
        security_id="s1",
        event_type="ticker_change",
        old_value="OLD",
        new_value="NEW",
        event_time="2025-01-01",
        public_available_time="2025-01-01",
        pat_provenance="source_reported",
        supersedes_id=baseline,
    )
    assert ticker_change
    assert identities.ticker_at("s1", "2026-01-01") == "NEW"


def test_raw_null_pat_identity_successor_does_not_consume_successor_slot(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "OLD", "NYSE", "One", None, None, "SUPPORTED", "2020-01-01", None),
        )
        baseline = db.execute(
            """INSERT INTO security_identity_event(
            security_id,event_type,new_value,event_time,public_available_time,
            pat_provenance,ingested_time
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                "s1",
                "baseline",
                "OLD",
                "2020-01-01",
                "2020-01-01",
                "source_reported",
                "2020-01-01",
            ),
        ).lastrowid
        with pytest.raises(sqlite3.IntegrityError, match="cannot backdate knowledge time"):
            db.execute(
                """INSERT INTO security_identity_event(
                security_id,event_type,old_value,new_value,event_time,public_available_time,
                pat_provenance,ingested_time,supersedes_id
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    "s1",
                    "ticker_change",
                    "OLD",
                    "UNPUBLISHED",
                    "2025-01-01",
                    None,
                    "observed_at_ingest",
                    "2025-01-02",
                    baseline,
                ),
            )

    identities = SecurityIdentityStore(database)
    assert identities.ticker_at("s1", "2025-01-02") == "OLD"
    identities.insert(
        security_id="s1",
        event_type="ticker_change",
        old_value="OLD",
        new_value="NEW",
        event_time="2025-01-01",
        public_available_time="2025-01-03",
        pat_provenance="source_reported",
        supersedes_id=baseline,
    )
    assert identities.ticker_at("s1", "2025-01-02") == "OLD"
    assert identities.ticker_at("s1", "2025-01-03") == "NEW"


def test_ticker_lineage_ignores_legacy_cross_domain_successors(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        for security_id in ("share", "delisted"):
            db.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    security_id,
                    "OLD",
                    "NYSE",
                    "One",
                    None,
                    None,
                    "SUPPORTED",
                    "2020-01-01",
                    None,
                ),
            )
        db.execute("DROP TRIGGER identity_supersession_valid")
        share_baseline = db.execute(
            """INSERT INTO security_identity_event(
            security_id,event_type,new_value,event_time,public_available_time,
            pat_provenance,ingested_time
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                "share",
                "baseline",
                "OLD",
                "2020-01-01",
                "2020-01-01",
                "source_reported",
                "2020-01-01",
            ),
        ).lastrowid
        db.execute(
            """INSERT INTO security_identity_event(
            security_id,event_type,old_value,new_value,event_time,public_available_time,
            pat_provenance,ingested_time,supersedes_id
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "share",
                "share_class_change",
                "A",
                "B",
                "2025-01-01",
                "2025-01-01",
                "source_reported",
                "2025-01-01",
                share_baseline,
            ),
        )
        ticker_change = db.execute(
            """INSERT INTO security_identity_event(
            security_id,event_type,old_value,new_value,event_time,public_available_time,
            pat_provenance,ingested_time
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                "delisted",
                "ticker_change",
                "OLDER",
                "OLD",
                "2020-01-01",
                "2020-01-01",
                "source_reported",
                "2020-01-01",
            ),
        ).lastrowid
        db.execute(
            """INSERT INTO security_identity_event(
            security_id,event_type,old_value,event_time,public_available_time,
            pat_provenance,ingested_time,supersedes_id
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                "delisted",
                "delisting",
                "OLD",
                "2025-01-01",
                "2025-01-01",
                "source_reported",
                "2025-01-01",
                ticker_change,
            ),
        )

    identities = SecurityIdentityStore(database)
    assert identities.ticker_at("share", "2020-01-01") == "OLD"
    assert identities.ticker_at("share", "2026-01-01") == "OLD"
    assert identities.ticker_at("delisted", "2026-01-01") == "OLD"


def test_experiment_reruns_and_attempts_append(tmp_path):
    database = initialized(tmp_path)
    registry = ExperimentRegistry(database)
    failed = registry.start("alpha", {"x": 1}, "hash", status="FAILED")
    succeeded = registry.start("alpha", {"x": 1}, "hash", status="SUCCEEDED")
    registry.record_attempt(failed, 1, "FAILED")
    registry.record_attempt(succeeded, 1, "SUCCEEDED")
    assert failed != succeeded
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT COUNT(*) FROM experiment_run").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM oos_evaluation_log").fetchone()[0] == 2


def test_snapshot_is_consistent_immutable_copy_and_read_only(tmp_path):
    database = initialized(tmp_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "ABC", "NYSE", "Co", None, None, "SUPPORTED", "2025-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("src", "filing", 1, "", "source_reported"),
        )
        db.execute(
            """INSERT INTO evidence_event(
                evidence_id,security_id,source_id,structured_fields,extraction_confidence,
                supersedes_evidence_id,withdrawn,content_hash,event_time,
                public_available_time,pat_provenance,ingested_time
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "e1",
                "s1",
                "src",
                '{"x":1}',
                1,
                None,
                0,
                "h1",
                "2025-01-01",
                "2025-01-02",
                "source_reported",
                "2025-01-03",
            ),
        )
    path = tmp_path / "snapshot.db"
    snapshot_id = create_snapshot(database, path)
    with database.connect() as db:
        db.execute(
            """INSERT INTO evidence_event(
                evidence_id,security_id,source_id,structured_fields,extraction_confidence,
                supersedes_evidence_id,withdrawn,content_hash,event_time,
                public_available_time,pat_provenance,ingested_time
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "e2",
                "s1",
                "src",
                '{"x":2}',
                1,
                None,
                0,
                "h2",
                "2025-01-01",
                "2025-01-02",
                "source_reported",
                "2025-01-04",
            ),
        )
    snapshot = open_snapshot_read_only(path)
    connection = snapshot.connection()
    try:
        assert connection.execute("SELECT COUNT(*) FROM evidence_event").fetchone()[0] == 1
        assert snapshot.manifest["snapshot_id"] == snapshot_id
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "INSERT INTO sealed_holdout(description,sealed_at) VALUES ('x','now')"
            )
    finally:
        connection.close()
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT snapshot_id FROM snapshot_version").fetchone()[0] == snapshot_id


def test_snapshot_rejects_live_path_overwrite_and_failed_publication(tmp_path, monkeypatch):
    database = initialized(tmp_path)
    with pytest.raises(ValueError, match="live database"):
        create_snapshot(database, database.path)
    existing = tmp_path / "existing.db"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_snapshot(database, existing)

    destination = tmp_path / "failed.db"

    def fail_replace(_source, _destination):
        raise OSError("simulated crash boundary")

    monkeypatch.setattr(snapshot_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="crash boundary"):
        create_snapshot(database, destination)
    assert not destination.exists()
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT status FROM snapshot_version").fetchone()[0] == "PENDING"


def test_snapshot_handle_rejects_replaced_file(tmp_path):
    first = initialized(tmp_path / "first")
    second = initialized(tmp_path / "second")
    first_path = tmp_path / "first.db"
    replacement_path = tmp_path / "replacement.db"
    create_snapshot(first, first_path)
    handle = open_snapshot_read_only(first_path)
    create_snapshot(second, replacement_path)
    os.replace(replacement_path, first_path)
    with pytest.raises(sqlite3.DatabaseError, match="identity does not match"):
        handle.connection()


def test_snapshot_handle_rejects_manifest_and_schema_tampering(tmp_path):
    database = initialized(tmp_path)
    paths = []
    for name in ("manifest", "view", "index", "trigger"):
        path = tmp_path / f"{name}.db"
        create_snapshot(database, path)
        paths.append(path)
    handles = [open_snapshot_read_only(path) for path in paths]
    mutations = (
        "UPDATE snapshot_manifest SET created_at='tampered'",
        "CREATE VIEW poison AS SELECT 1 AS poisoned",
        "DROP INDEX evidence_pit_idx",
        "DROP TRIGGER evidence_no_delete",
    )
    for path, handle, mutation in zip(paths, handles, mutations, strict=True):
        with sqlite3.connect(path) as db:
            db.execute(mutation)
        with pytest.raises(sqlite3.DatabaseError):
            handle.connection()


def test_published_pending_snapshot_is_refused_then_reconciled(tmp_path):
    database = initialized(tmp_path)
    first_path = tmp_path / "first.db"
    snapshot_id = create_snapshot(database, first_path)
    with database.connect() as db:
        db.execute(
            "UPDATE snapshot_version SET status='PENDING' WHERE snapshot_id=?", (snapshot_id,)
        )
    with pytest.raises(sqlite3.DatabaseError, match="not READY"):
        open_snapshot_read_only(first_path)
    create_snapshot(database, tmp_path / "second.db")
    open_snapshot_read_only(first_path).connection().close()
    with database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT status FROM snapshot_version WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()[0]
            == "READY"
        )


def test_cli_init_and_check(tmp_path):
    path = tmp_path / "cli.db"
    init = subprocess.run(
        [sys.executable, "-m", "tradehub_research.cli", "init", "--db", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    check = subprocess.run(
        [sys.executable, "-m", "tradehub_research.cli", "check", "--db", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert init.returncode == check.returncode == 0
    assert json.loads(check.stdout)["ok"] is True


def test_ra00_is_deterministic_pass_with_required_shape():
    result = run_pack("RA-00")
    payload = result.to_safe_dict()
    assert result.status == Status.PASS
    assert result.pack_id == "RA-00"
    assert result.run_id.startswith("ra00-")
    assert {"run_id", "status", "assertions", "commit_sha"} <= payload.keys()
    assert all(a["status"] in {s.value for s in Status} for a in payload["assertions"])


def test_research_acceptance_registry_is_an_explicit_whitelist():
    from tradehub_research.acceptance.packs.ra00 import ASSERTIONS as RA00_ASSERTIONS
    from tradehub_research.acceptance.packs.ra01 import ASSERTIONS as RA01_ASSERTIONS
    from tradehub_research.acceptance.packs.ra02 import ASSERTIONS as RA02_ASSERTIONS
    from tradehub_research.acceptance.runner import PACK_REGISTRY

    assert PACK_REGISTRY == {
        "RA-00": RA00_ASSERTIONS,
        "RA-01": RA01_ASSERTIONS,
        "RA-02": RA02_ASSERTIONS,
    }
    assert [assertion_id for assertion_id, _ in PACK_REGISTRY["RA-00"]] == [
        "schema.version",
        "db.fresh_init",
        "config.research_only",
        "pit.fixture_timing",
        "pat.unknown_behavior",
        "evidence.append_only_supersession",
        "evidence.idempotent_ingestion",
        "pit.identity_membership",
        "pit.retraction",
        "cli.init",
    ]


def test_research_acceptance_registry_drives_prefix_and_pack_id(monkeypatch):
    from tradehub_research.acceptance.runner import PACK_REGISTRY

    monkeypatch.setitem(PACK_REGISTRY, "RA-01", [])
    result = run_pack("RA-01")

    assert result.status == Status.PASS
    assert result.pack_id == "RA-01"
    assert result.run_id.startswith("ra01-")


def test_unknown_research_acceptance_pack_fails_closed():
    result = run_pack("RA-99")

    assert result.status == Status.FAIL
    assert result.pack_id == "RA-99"
    actual = [(assertion.id, assertion.status, assertion.detail) for assertion in result.assertions]
    assert actual == [("pack.lookup", Status.FAIL, "unknown pack")]


def test_installed_ra00_command_runs_outside_repository(tmp_path):
    executable = os.path.join(sys.prefix, "bin", "tradehub-research-acceptance")
    result = subprocess.run(
        [executable, "RA-00"], cwd=tmp_path, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["commit_sha"] != "unavailable"
    assert [(item["id"], item["status"]) for item in payload["assertions"]] == [
        (assertion_id, "PASS")
        for assertion_id in (
            "schema.version",
            "db.fresh_init",
            "config.research_only",
            "pit.fixture_timing",
            "pat.unknown_behavior",
            "evidence.append_only_supersession",
            "evidence.idempotent_ingestion",
            "pit.identity_membership",
            "pit.retraction",
            "cli.init",
        )
    ]


def test_sdist_to_wheel_embeds_commit_for_outside_repository_execution(tmp_path):
    root = os.path.dirname(os.path.dirname(__file__))
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    distribution_dir = tmp_path / "dist"
    built = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(distribution_dir)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin" / "python"
    assert next(distribution_dir.glob("*.tar.gz"))
    wheel = next(distribution_dir.glob("*.whl"))
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    result = subprocess.run(
        [str(environment / "bin" / "tradehub-research-acceptance"), "RA-00"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    commit_sha = json.loads(result.stdout)["commit_sha"]
    assert re.fullmatch(r"[0-9a-f]{40}", commit_sha)
    assert commit_sha == expected_sha


def test_package_has_no_tradehub_imports():
    root = os.path.dirname(os.path.dirname(__file__))
    package = os.path.join(root, "tradehub_research")
    pattern = re.compile(r"(?:from|import) tradehub(?:\.|\s|$)")
    assert pattern.search("import tradehub_research") is None

    matches = []
    for directory, subdirectories, filenames in os.walk(package):
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            with open(path, encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if pattern.search(line):
                        matches.append(f"{path}:{line_number}:{line.rstrip()}")

    assert not matches, "\n".join(matches)

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

from tradehub_research.acceptance.runner import run_pack
from tradehub_research.acceptance.schema import Status
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.experiment import ExperimentRegistry
from tradehub_research.schema import PHASE_0_SCHEMA_VERSION
from tradehub_research.snapshot import create_snapshot, open_snapshot_read_only


def initialized(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.init()
    return database


def test_schema_and_migration_are_idempotent(tmp_path):
    database = initialized(tmp_path)
    assert database.migrate() == PHASE_0_SCHEMA_VERSION
    assert database.check()["ok"]
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


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
            "public_available_time,pat_provenance) VALUES (?,?,?,?,?,?,?)",
            ("s1", "ticker_change", "OLD", "NEW", "2025-02-01", "2025-02-01", "source_reported"),
        )
        db.execute(
            "INSERT INTO security_identity_event("
            "security_id,event_type,old_value,new_value,event_time,"
            "public_available_time,pat_provenance) VALUES (?,?,?,?,?,?,?)",
            (
                "s1",
                "delisting",
                "listed",
                "delisted",
                "2025-04-01",
                "2025-03-15",
                "source_reported",
            ),
        )
        db.execute(
            "INSERT INTO universe_membership("
            "security_id,price,market_cap,avg_dollar_volume,price_eligible,"
            "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s1", 20, 1e9, 1e7, 1, 1, 1, 1, "2025-01-01", "2025-04-01"),
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
            "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
            "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
    try:
        assert snapshot.execute("SELECT COUNT(*) FROM evidence_event").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            snapshot.execute("INSERT INTO sealed_holdout(description,sealed_at) VALUES ('x','now')")
    finally:
        snapshot.close()
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT snapshot_id FROM snapshot_version").fetchone()[0] == snapshot_id


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
    assert {"run_id", "status", "assertions", "commit_sha"} <= payload.keys()
    assert all(a["status"] in {s.value for s in Status} for a in payload["assertions"])


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

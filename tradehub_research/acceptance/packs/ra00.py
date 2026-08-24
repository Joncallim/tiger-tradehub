from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.schema import PHASE_0_SCHEMA_VERSION
from tradehub_research.universe import UniverseMembershipStore


def seed(database: ResearchDB) -> EvidenceStore:
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("sec", "ABC", "NYSE", "ABC Inc", None, None, "SUPPORTED", "2025-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("src", "filing", 1, "", "source_reported"),
        )
    return EvidenceStore(database)


def schema_version(tmp: Path) -> None:
    database = ResearchDB(tmp / "schema.db")
    database.init()
    assert database.schema_version() == PHASE_0_SCHEMA_VERSION


def fresh_init(tmp: Path) -> None:
    database = ResearchDB(tmp / "fresh.db")
    database.init()
    assert database.check()["ok"]


def research_only_config(tmp: Path) -> None:
    code = (
        "from tradehub_research.config import ResearchSettings; print(ResearchSettings().db_path)"
    )
    env = {
        "PATH": "",
        "RESEARCH_DB_PATH": str(tmp / "only.db"),
        "TIGEROPEN_ID": "forbidden",
        "TRADEHUB_API_TOKEN": "forbidden",
    }
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0 and str(tmp / "only.db") in result.stdout


def pit_timing(tmp: Path) -> None:
    store = seed(ResearchDB(tmp / "pit.db"))
    first = store.insert(
        security_id="sec",
        source_id="src",
        structured_fields={"v": 1},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-05",
        pat_provenance="source_reported",
        ingested_time="2025-01-10",
    )
    assert not store.historical("2025-01-04")
    assert [r["evidence_id"] for r in store.historical("2025-01-05")] == [first]


def pat_unknown(tmp: Path) -> None:
    store = seed(ResearchDB(tmp / "unknown.db"))
    item = store.insert(
        security_id="sec",
        source_id="src",
        structured_fields={"v": 1},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time=None,
        pat_provenance="unknown",
    )
    assert not store.historical("2099-01-01") and store.current()[0]["evidence_id"] == item


def supersession(tmp: Path) -> None:
    store = seed(ResearchDB(tmp / "super.db"))
    original = store.insert(
        security_id="sec",
        source_id="src",
        structured_fields={"v": 1},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-05",
        pat_provenance="source_reported",
    )
    correction = store.insert(
        security_id="sec",
        source_id="src",
        structured_fields={"v": 2},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-08",
        pat_provenance="source_reported",
        supersedes_evidence_id=original,
    )
    assert store.historical("2025-01-06")[0]["evidence_id"] == original
    assert store.historical("2025-01-09")[0]["evidence_id"] == correction
    try:
        store.insert(
            security_id="sec",
            source_id="src",
            structured_fields={"v": 3},
            extraction_confidence=1,
            event_time="2025-01-01",
            public_available_time="2025-01-04",
            pat_provenance="source_reported",
            supersedes_evidence_id=correction,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("backdated correction was accepted")
    with store.database.connect() as db:
        try:
            db.execute(
                "UPDATE evidence_event SET structured_fields='{}' WHERE evidence_id=?", (original,)
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("original was mutable")


def idempotence(tmp: Path) -> None:
    store = seed(ResearchDB(tmp / "idem.db"))
    args = dict(
        security_id="sec",
        source_id="src",
        structured_fields={"v": 1},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-05",
        pat_provenance="source_reported",
    )
    assert store.insert(**args) == store.insert(**args)


def identity_membership(tmp: Path) -> None:
    database = ResearchDB(tmp / "identity.db")
    seed(database)
    with database.connect() as db:
        db.execute(
            "INSERT INTO security_identity_event("
            "security_id,event_type,old_value,new_value,event_time,"
            "public_available_time,pat_provenance) VALUES (?,?,?,?,?,?,?)",
            ("sec", "ticker_change", "OLD", "ABC", "2025-02-01", "2025-02-01", "source_reported"),
        )
    memberships = UniverseMembershipStore(database)
    memberships.insert(
        security_id="sec",
        price=10,
        market_cap=1e9,
        avg_dollar_volume=1e7,
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
        valid_from="2020-01-01",
        valid_to="2025-04-01",
        knowledge_time="2025-01-01",
        pat_provenance="source_reported",
    )
    assert not memberships.pit_valid("sec", "2020-02-01")
    assert len(memberships.pit_valid("sec", "2025-02-01")) == 1
    assert not memberships.pit_valid("sec", "2025-05-01")
    memberships.insert(
        security_id="sec",
        price=None,
        market_cap=None,
        avg_dollar_volume=None,
        price_eligible=False,
        market_cap_eligible=False,
        liquidity_eligible=False,
        eligible=False,
        valid_from="2025-05-01",
        knowledge_time="2025-05-01",
        pat_provenance="unknown",
    )
    assert not memberships.pit_valid("sec", "2025-06-01")
    assert memberships.current("sec")[0]["pat_provenance"] == "unknown"


def retraction(tmp: Path) -> None:
    store = seed(ResearchDB(tmp / "retraction.db"))
    original = store.insert(
        security_id="sec",
        source_id="src",
        structured_fields={"v": 1},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-05",
        pat_provenance="source_reported",
    )
    store.insert(
        security_id="sec",
        source_id="src",
        structured_fields={},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-08",
        pat_provenance="source_reported",
        supersedes_evidence_id=original,
        withdrawn=True,
    )
    assert store.historical("2025-01-09") == []
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_event").fetchone()[0] == 2


def cli_init(tmp: Path) -> None:
    path = tmp / "cli.db"
    result = subprocess.run(
        [sys.executable, "-m", "tradehub_research.cli", "init", "--db", str(path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0 and ResearchDB(path).check()["ok"]


ASSERTIONS = (
    ("schema.version", schema_version),
    ("db.fresh_init", fresh_init),
    ("config.research_only", research_only_config),
    ("pit.fixture_timing", pit_timing),
    ("pat.unknown_behavior", pat_unknown),
    ("evidence.append_only_supersession", supersession),
    ("evidence.idempotent_ingestion", idempotence),
    ("pit.identity_membership", identity_membership),
    ("pit.retraction", retraction),
    ("cli.init", cli_init),
)

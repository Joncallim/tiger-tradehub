from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tradehub_research.committee.pack import EvidencePackBuilder, PackBuildError
from tradehub_research.committee.store import CommitteeStore, ComparatorSpec, ScoringSpec
from tradehub_research.db import ResearchDB
from tradehub_research.schema import MIGRATIONS, PHASE_0_SCHEMA_VERSION
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import ScreenResult, ScreenSpec, canonical_json

# ruff: noqa: E501 -- fixture SQL mirrors complete immutable table layouts.


def _fixture(path: Path, *, missing_accession: bool = False) -> tuple[ResearchDB, str]:
    database = ResearchDB(path)
    database.migrate()
    spec = ScreenSpec("valuation", "value", 1, 1, {}, [], "test")
    result = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=spec.config_hash,
        raw_features={"note": "ok"},
        evidence_ids=["x1", "x2", "event1", "event2"],
        reason_codes=[],
        sufficient_data=True,
        passed=True,
        confidence=0.8,
        data_quality=0.9,
    )
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("sec", "TST", "NYSE", "Test", "Tech", None, "SUPPORTED", "2024-01-01Z", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("src", "filing", 1, None, "source_reported"),
        )
        fields = {"record_type": "xbrl_fact", "value": 1}
        if not missing_accession:
            fields["accession"] = "acc"
        events = (
            ("x1", fields, None),
            ("x2", {**fields, "value": 2}, "x1"),
            ("event1", {"record_type": "news", "text": "a"}, None),
            ("event2", {"record_type": "news", "text": "b"}, None),
        )
        for evidence_id, structured, predecessor in events:
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json(structured),
                    1.0,
                    predecessor,
                    0,
                    evidence_id + "hash",
                    evidence_id,
                    "2025-01-01T00:00:00Z",
                    "2025-01-02T00:00:00Z",
                    "source_reported",
                    "2025-01-03T00:00:00Z",
                ),
            )
        db.execute(
            "INSERT INTO evidence_cluster VALUES (?,?,?)", ("cl", "cluster", "2025-01-02T00:00:00Z")
        )
        db.executemany(
            "INSERT INTO evidence_cluster_member VALUES (?,?)", (("event1", "cl"), ("event2", "cl"))
        )
        db.execute(
            "INSERT INTO screen_definition VALUES (?,?,?,?,?,?)",
            (
                spec.config_hash,
                spec.family,
                spec.screen_id,
                spec.screen_version,
                spec.canonical_json(),
                "2025-01-01Z",
            ),
        )
        db.execute(
            "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,input_view_hash,expected_security_count,status,failure_json,started_at,finished_at,flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run",
                "2025-02-01T00:00:00Z",
                "universe",
                "[]",
                "manifest",
                "{}",
                "funnel",
                None,
                "view",
                1,
                "RUNNING",
                None,
                "2025-01-01Z",
                None,
                "[]",
            ),
        )
        db.execute(
            "INSERT INTO screen_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result.screen_result_id,
                "run",
                "sec",
                spec.config_hash,
                canonical_json(result.raw_features),
                canonical_json(result.evidence_ids),
                "[]",
                1,
                1,
                0.8,
                0.9,
                result.result_hash,
                "2025-01-02Z",
            ),
        )
        db.execute(
            "UPDATE pipeline_run SET status='COMPLETE',finished_at='2025-01-02Z' WHERE run_id='run'"
        )
        db.execute(
            "INSERT INTO candidate VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate",
                "run",
                "sec",
                1,
                "[]",
                canonical_json([result.screen_result_id]),
                "{}",
                0,
                None,
                None,
                None,
                "2025-01-02Z",
            ),
        )
    return database, "candidate"


def test_schema_v8_fresh_and_append_only(tmp_path):
    database = ResearchDB(tmp_path / "fresh.db")
    assert database.migrate() == PHASE_0_SCHEMA_VERSION == 8
    with database.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"evidence_pack", "committee_run", "score_snapshot"} <= tables
        assert "state" not in {row[1] for row in db.execute("PRAGMA table_info(committee_run)")}


def test_schema_v7_upgrade_is_additive(tmp_path):
    path = tmp_path / "upgrade.db"
    database = ResearchDB(path)
    with database.connect() as db:
        db.execute(
            "CREATE TABLE schema_version(version_id INTEGER PRIMARY KEY,applied_at TEXT NOT NULL,description TEXT NOT NULL)"
        )
        for version, description, sql in MIGRATIONS[:7]:
            db.executescript(sql)
            db.execute("INSERT INTO schema_version VALUES (?,?,?)", (version, "now", description))
    assert database.migrate() == 8


def test_pack_exact_groups_supersession_and_hash_retry(tmp_path):
    database, candidate_id = _fixture(tmp_path / "pack.db")
    first = EvidencePackBuilder(database).build(candidate_id)
    second = EvidencePackBuilder(database).build(candidate_id)
    assert first == second
    rows = {row["evidence_id"]: row for row in first.body["evidence"]}
    assert rows["x1"]["underlying_group"] == rows["x2"]["underlying_group"] == "xbrl:src:acc"
    assert rows["x1"]["superseded_within_pack_by"] == "x2"
    assert (
        rows["event1"]["underlying_group"] == rows["event2"]["underlying_group"] == "cluster:src:cl"
    )
    assert "ordinal" not in first.body["candidate"]


def test_pack_ignores_unreferenced_future_evidence(tmp_path):
    database, candidate_id = _fixture(tmp_path / "future.db")
    before = EvidencePackBuilder(database).build(candidate_id)
    with database.connect() as db:
        db.execute(
            "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "future",
                "sec",
                "src",
                '{"record_type":"news"}',
                1,
                None,
                0,
                "h",
                "future",
                "2025-01-01Z",
                "2026-01-01Z",
                "source_reported",
                "2026-01-01Z",
            ),
        )
    assert EvidencePackBuilder(database).build(candidate_id).pack_hash == before.pack_hash


def test_passing_xbrl_requires_accession(tmp_path):
    database, candidate_id = _fixture(tmp_path / "bad.db", missing_accession=True)
    with pytest.raises(PackBuildError, match="UNGROUPABLE_XBRL"):
        EvidencePackBuilder(database).build(candidate_id)
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM evidence_pack").fetchone()[0] == 0


def test_pack_insert_or_verify_detects_mismatch(tmp_path):
    database, candidate_id = _fixture(tmp_path / "det.db")
    EvidencePackBuilder(database).build(candidate_id)
    with database.connect() as db:
        db.execute("DROP TRIGGER evidence_pack_no_update")
        db.execute("UPDATE evidence_pack SET body_json='{}'")
    with pytest.raises(DeterminismError):
        EvidencePackBuilder(database).build(candidate_id)


def test_pack_records_string_truncation(tmp_path):
    database, candidate_id = _fixture(tmp_path / "truncate.db")
    fields = canonical_json({"record_type": "news", "text": "z" * 600})
    with database.connect() as db:
        db.execute("DROP TRIGGER evidence_no_update")
        db.execute(
            "UPDATE evidence_event SET structured_fields=? WHERE evidence_id='event1'", (fields,)
        )
    pack = EvidencePackBuilder(database).build(candidate_id)
    event = next(row for row in pack.body["evidence"] if row["evidence_id"] == "event1")
    assert len(event["structured_fields"]["text"]) == 512
    assert any(item["kind"] == "string" for item in pack.body["bounds"]["truncations"])


def test_pack_rejects_oversize_structured_fields_without_silent_trim(tmp_path):
    database, candidate_id = _fixture(tmp_path / "large.db")
    fields = canonical_json({f"key-{index:02}": "z" * 512 for index in range(32)})
    with database.connect() as db:
        db.execute("DROP TRIGGER evidence_no_update")
        db.execute(
            "UPDATE evidence_event SET structured_fields=? WHERE evidence_id='event1'", (fields,)
        )
    with pytest.raises(PackBuildError, match="PACK_TOO_LARGE"):
        EvidencePackBuilder(database).build(candidate_id)


def test_registry_specs_and_idempotence(tmp_path):
    database, _ = _fixture(tmp_path / "registry.db")
    store = CommitteeStore(database)
    assert store.ensure_registry_rows() == store.ensure_registry_rows()
    assert len(ComparatorSpec().as_dict()["taxonomy"]) == 30
    assert sum(ScoringSpec().as_dict()["weights"].values()) == 90


def test_v8_artifact_tables_are_append_only(tmp_path):
    database, _ = _fixture(tmp_path / "append.db")
    CommitteeStore(database).ensure_registry_rows()
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE scoring_version SET description='changed'")


def _committee(tmp_path: Path) -> tuple[CommitteeStore, str, str]:
    database, candidate_id = _fixture(tmp_path)
    pack = EvidencePackBuilder(database).build(candidate_id)
    store = CommitteeStore(database)
    comparator, scoring = store.ensure_registry_rows()
    run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=pack.pack_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator,
        scoring_config_hash=scoring,
        prompt_versions={"neutral": "v1"},
        assessment_schema_version=1,
        provider_routes={"a": "one"},
    )
    return store, run, pack.pack_hash


def test_committee_identity_excludes_routes_and_resumes(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "committee.db")
    comparator, scoring = ComparatorSpec().config_hash, ScoringSpec().config_hash
    retried = store.create_or_resume_committee_run(
        candidate_id="candidate",
        pack_hash=pack_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator,
        scoring_config_hash=scoring,
        prompt_versions={"neutral": "v1"},
        assessment_schema_version=1,
        provider_routes={"a": "different"},
    )
    assert retried == run


def test_transitions_derive_state_and_reject_wrong_from(tmp_path):
    store, run, _ = _committee(tmp_path / "transition.db")
    store.record_transition(run, None, "PENDING_NEUTRALS", "CREATED")
    assert store.current_state(run) == "PENDING_NEUTRALS"
    with pytest.raises(ValueError):
        store.record_transition(run, None, "READY_TO_SCORE", "BAD")


def test_transition_identical_retry_is_idempotent(tmp_path):
    store, run, _ = _committee(tmp_path / "transition-retry.db")
    first = store.record_transition(run, None, "PENDING_NEUTRALS", "CREATED")
    assert store.record_transition(run, None, "PENDING_NEUTRALS", "CREATED") == first


def _assessment(pack_hash: str) -> dict[str, object]:
    return {
        "candidate_id": "candidate",
        "pack_hash": pack_hash,
        "provider": "p",
        "model_id": "m",
        "prompt_version": "v1",
        "assessment_schema_version": 1,
        "taxonomy_version": 1,
        "model_route": "r",
        "billing_class": "local",
        "claims": [],
        "cited_evidence_ids": [],
        "missing_evidence": [],
        "thesis": {},
        "confidence": 0.5,
        "uncertainty": 0.5,
        "usage": {},
        "cost": {},
        "evaluation_time": "2025-02-01Z",
        "submitted_at": "2025-02-02Z",
    }


def test_assessment_validates_atomically_and_role_is_unique(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "assessment.db")
    payload = _assessment(pack_hash)
    with pytest.raises(ValueError):
        store.insert_assessment(
            committee_run_id=run,
            role="neutral_analyst_a",
            payload=payload,
            validate=lambda _: (_ for _ in ()).throw(ValueError("invalid")),
        )
    first = store.insert_assessment(
        committee_run_id=run, role="neutral_analyst_a", payload=payload, validate=lambda _: None
    )
    assert (
        store.insert_assessment(
            committee_run_id=run, role="neutral_analyst_a", payload=payload, validate=lambda _: None
        )
        == first
    )
    changed = dict(payload, confidence=0.6)
    with pytest.raises(DeterminismError):
        store.insert_assessment(
            committee_run_id=run, role="neutral_analyst_a", payload=changed, validate=lambda _: None
        )


def test_call_attempt_slot_is_insert_or_verify(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "attempt.db")
    payload = {
        "provider": "p",
        "model_id": "m",
        "model_route": "r",
        "billing_class": "local",
        "prompt_version": "v1",
        "prompt_template_hash": "template",
        "pack_hash": pack_hash,
        "outcome": "ACCEPTED",
        "usage": {},
        "cost": {},
        "diagnostic_hash": None,
        "diagnostic_excerpt": None,
        "requested_at": "2025-02-01Z",
        "completed_at": "2025-02-01Z",
    }
    first = store.insert_call_attempt(
        committee_run_id=run, role="neutral_analyst_a", attempt_number=1, **payload
    )
    assert (
        store.insert_call_attempt(
            committee_run_id=run, role="neutral_analyst_a", attempt_number=1, **payload
        )
        == first
    )
    with pytest.raises(DeterminismError):
        store.insert_call_attempt(
            committee_run_id=run,
            role="neutral_analyst_a",
            attempt_number=1,
            **dict(payload, model_id="different"),
        )

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tradehub_research.committee.api import app, get_database, get_settings
from tradehub_research.committee.assessment import AssessmentValidationError
from tradehub_research.committee.comparator import Comparator
from tradehub_research.committee.pack import EvidencePackBuilder, PackBuildError
from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.committee.scoring import Scorer
from tradehub_research.committee.store import CommitteeStore, ComparatorSpec, ScoringSpec
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.schema import MIGRATIONS, PHASE_0_SCHEMA_VERSION
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import ScreenResult, ScreenSpec, canonical_json
from tradehub_research.universe import SecurityIdentityStore

# ruff: noqa: E501 -- fixture SQL mirrors complete immutable table layouts.


def _fixture(
    path: Path, *, missing_accession: bool = False, as_of: str = "2025-02-01T00:00:00Z"
) -> tuple[ResearchDB, str]:
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
                as_of,
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


def test_schema_v9_fresh_and_append_only(tmp_path):
    database = ResearchDB(tmp_path / "fresh.db")
    assert database.migrate() == PHASE_0_SCHEMA_VERSION == 9
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
    assert database.migrate() == 9


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
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
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
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
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
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "source": "UNKNOWN",
        },
        "cost": {"amount": None, "currency": None, "source": "UNKNOWN"},
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
    work_id = store.insert_work(
        committee_run_id=run,
        role="neutral_analyst_a",
        attempt_number=1,
        pack_hash=pack_hash,
        prompt_version="v1",
        assessment_schema_version=1,
        taxonomy_version=1,
        focus_hash=None,
        focus=None,
    )
    payload = {
        "provider": "p",
        "model_id": "m",
        "model_route": "r",
        "billing_class": "local",
        "prompt_version": "v1",
        "prompt_template_hash": "template",
        "pack_hash": pack_hash,
        "outcome": "accepted",
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "source": "UNKNOWN",
        },
        "cost": {"amount": None, "currency": None, "source": "UNKNOWN"},
        "diagnostic_hash": None,
        "diagnostic_excerpt": "provider error: Bearer TOP-SECRET",
        "requested_at": "2025-02-01Z",
        "completed_at": "2025-02-01Z",
    }
    first = store.insert_call_attempt(
        work_id=work_id,
        committee_run_id=run,
        role="neutral_analyst_a",
        attempt_number=1,
        **payload,
    )
    assert (
        store.insert_call_attempt(
            work_id=work_id,
            committee_run_id=run,
            role="neutral_analyst_a",
            attempt_number=1,
            **payload,
        )
        == first
    )
    with store.database.connect(read_only=True) as db:
        excerpt = db.execute(
            "SELECT diagnostic_excerpt FROM model_call_attempt WHERE attempt_id=?", (first,)
        ).fetchone()[0]
    assert "TOP-SECRET" not in excerpt and "[REDACTED]" in excerpt
    with pytest.raises(DeterminismError):
        store.insert_call_attempt(
            work_id=work_id,
            committee_run_id=run,
            role="neutral_analyst_a",
            attempt_number=1,
            **dict(payload, model_id="different"),
        )


def test_committee_http_auth_create_status_work_and_score_reads(tmp_path):
    database, candidate_id = _fixture(tmp_path / "api.db")
    settings = ResearchSettings(api_token="research-secret")
    app.dependency_overrides[get_database] = lambda: database
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)
        assert (
            client.post("/committee-runs", json={"candidate_id": candidate_id}).status_code == 401
        )
        headers = {"Authorization": "Bearer research-secret"}
        created = client.post(
            "/committee-runs", json={"candidate_id": candidate_id}, headers=headers
        )
        assert created.status_code == 200
        run_id = created.json()["committee_run_id"]
        assert {item["role"] for item in created.json()["work"]} == {
            "neutral_analyst_a",
            "neutral_analyst_b",
        }
        assert all(
            {
                "work_id",
                "pack_hash",
                "prompt_version",
                "assessment_schema_version",
                "taxonomy_version",
            }
            <= set(item)
            for item in created.json()["work"]
        )
        assert (
            client.post(
                "/committee-runs", json={"candidate_id": candidate_id}, headers=headers
            ).json()["committee_run_id"]
            == run_id
        )
        assert (
            client.get(f"/committee-runs/{run_id}", headers=headers).json()["state"]
            == "PENDING_NEUTRALS"
        )
        work = client.get(f"/committee-runs/{run_id}/work", headers=headers).json()["work"]
        assert work["role"] == "neutral_analyst_a"
        assert "neutral_analyst_b" not in str(work)
        assert client.get("/score-snapshots/missing", headers=headers).status_code == 404
        assert client.get(
            f"/candidates/{candidate_id}/score-snapshots", headers=headers
        ).json() == {"score_snapshots": []}
    finally:
        app.dependency_overrides.clear()


def test_http_rejects_oversize_attempt_telemetry_without_ledger_write(tmp_path):
    store, run, _ = _committee(tmp_path / "api-attempt-bounds.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    work = router.get_work(run)
    settings = ResearchSettings(api_token="research-secret")
    app.dependency_overrides[get_database] = lambda: store.database
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)
        headers = {"Authorization": "Bearer research-secret"}
        giant_amount = _attempt(work, "unavailable")
        giant_amount["cost"] = {
            "amount": "9" * 100_000,
            "currency": "USD",
            "source": "SELF_REPORTED",
        }
        assert (
            client.post(
                f"/committee-runs/{run}/assessments", json=giant_amount, headers=headers
            ).status_code
            == 422
        )
        giant_tokens = _attempt(work, "timeout")
        giant_tokens["usage"] = {
            "input_tokens": 1_000_000_001,
            "output_tokens": 0,
            "cached_tokens": 0,
            "source": "SELF_REPORTED",
        }
        assert (
            client.post(
                f"/committee-runs/{run}/assessments", json=giant_tokens, headers=headers
            ).status_code
            == 422
        )
    finally:
        app.dependency_overrides.clear()
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM model_call_attempt").fetchone()[0] == 0


def test_atomic_validated_neutral_submission_advances_after_both(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "submit-route.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)

    def valid(role: str) -> dict[str, object]:
        return {
            **_assessment(pack_hash),
            "role": role,
            "evaluation_time": "2025-02-01T00:00:00Z",
            "thesis": {
                "summary": "summary",
                "upside_mechanism": "upside",
                "downside_mechanism": "downside",
                "thesis_break_conditions": [],
            },
        }

    assert router.submit(run, valid("neutral_analyst_a"))["state"] == "PENDING_NEUTRALS"
    analyst_b = valid("neutral_analyst_b")
    analyst_b["provider"] = "provider-b"
    assert router.submit(run, analyst_b)["state"] == "RED_TEAM_REQUIRED"
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM model_assessment WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(*) FROM comparison_report WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 1
        )


def test_aligned_neutrals_score_deterministically(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "aligned-score.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)

    def aligned(role: str, provider: str) -> dict[str, object]:
        return {
            **_assessment(pack_hash),
            "role": role,
            "provider": provider,
            "evaluation_time": "2025-02-01T00:00:00Z",
            "claims": [
                {
                    "claim_key": "valuation_vs_history",
                    "claim_type": "fact",
                    "direction": "bullish",
                    "statement": "supported",
                    "materiality": 3,
                    "uncertainty": 0.2,
                    "cited_evidence_ids": ["x2"],
                    "contradictory_evidence_ids": [],
                    "falsification_condition": None,
                }
            ],
            "cited_evidence_ids": ["x2"],
            "thesis": {
                "summary": "summary",
                "upside_mechanism": "upside",
                "downside_mechanism": "downside",
                "thesis_break_conditions": [],
            },
        }

    router.submit(run, aligned("neutral_analyst_a", "provider-a"))
    result = router.submit(run, aligned("neutral_analyst_b", "provider-b"))
    assert result["state"] == "SCORED"
    with store.database.connect(read_only=True) as db:
        snapshot = db.execute(
            "SELECT conviction,committee_agreement FROM score_snapshot WHERE committee_run_id=?",
            (run,),
        ).fetchone()
    assert tuple(snapshot) == (10, 1.0)


def _valid_assessment(
    pack_hash: str,
    role: str,
    provider: str,
    claims: list[dict] | None = None,
) -> dict[str, object]:
    claims = claims or []
    citations = sorted(
        {evidence_id for claim in claims for evidence_id in claim.get("cited_evidence_ids", [])}
    )
    value = {
        **_assessment(pack_hash),
        "role": role,
        "provider": provider,
        "evaluation_time": "2025-02-01T00:00:00Z",
        "claims": claims,
        "cited_evidence_ids": citations,
        "thesis": {
            "summary": "summary",
            "upside_mechanism": "upside",
            "downside_mechanism": "downside",
            "thesis_break_conditions": [],
        },
    }
    value.pop("submitted_at")
    return value


def _claim(key: str, direction: str, *, evidence: str = "x2") -> dict:
    return {
        "claim_key": key,
        "claim_type": "fact",
        "direction": direction,
        "statement": f"{key} {direction}",
        "materiality": 3,
        "uncertainty": 0.2,
        "cited_evidence_ids": [evidence],
        "contradictory_evidence_ids": [],
        "falsification_condition": None,
    }


def _attempt(work: dict, outcome: str, assessment: dict | None = None, provider: str = "p") -> dict:
    value = {
        "work_id": work["work_id"],
        "outcome": outcome,
        "provider": provider,
        "model_id": "model",
        "model_route": "route",
        "billing_class": "local",
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "cached_tokens": None,
            "source": "UNKNOWN",
        },
        "cost": {"amount": None, "currency": None, "source": "UNKNOWN"},
    }
    if assessment is not None:
        value["assessment"] = assessment
        for name in ("provider", "model_id", "model_route", "billing_class", "usage", "cost"):
            value[name] = assessment[name]
    return value


def _disputed_committee(tmp_path: Path, *, include_shared: bool = False):
    store, run, pack_hash = _committee(tmp_path)
    router = CommitteeRouter(store.database)
    router.initialize(run)
    keys = ["valuation_vs_history", "earnings_quality", "margin_durability"]
    claims_a = [_claim(key, "bullish") for key in keys]
    claims_b = [_claim(key, "bearish") for key in keys]
    if include_shared:
        claims_a.append(_claim("revenue_inflection", "bullish", evidence="event1"))
        claims_b.append(_claim("revenue_inflection", "bullish", evidence="event1"))
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_a", "provider-a", claims_a))
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_b", "provider-b", claims_b))
    return store, router, run, pack_hash


def _verdict_assessment(pack_hash: str, role: str, provider: str, verdicts: list[dict]) -> dict:
    return _valid_assessment(pack_hash, role, provider, verdicts)


def _verdict(item_id: str, verdict: str = "resolved_for_a") -> dict:
    return {
        "item_id": item_id,
        "verdict": verdict,
        "statement": "typed verdict",
        "cited_evidence_ids": ["x2"],
    }


def test_targeted_focus_exact_coverage_and_atomic_rejection(tmp_path):
    store, router, run, pack_hash = _disputed_committee(tmp_path / "partial.db")
    work = router.get_work(run)
    assert work["role"] == "red_team" and len(work["focus"]["items"]) == 3
    partial = _verdict_assessment(
        pack_hash, "red_team", "provider-red", [_verdict(work["focus"]["items"][0]["item_id"])]
    )
    with pytest.raises(ValueError, match="exactly cover"):
        router.submit(run, _attempt(work, "accepted", partial))
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM model_assessment WHERE committee_run_id=? AND role='red_team'",
                (run,),
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM dispute_resolution WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM score_snapshot WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 0
        )


def test_shared_item_verdict_is_rejected_before_artifact_write(tmp_path):
    store, router, run, pack_hash = _disputed_committee(tmp_path / "shared.db", include_shared=True)
    work = router.get_work(run)
    with store.database.connect(read_only=True) as db:
        buckets = json.loads(
            db.execute(
                "SELECT report_json FROM comparison_report WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
        )
    shared_id = buckets["SHARED"][0]["item_id"]
    bad = _verdict_assessment(pack_hash, "red_team", "provider-red", [_verdict(shared_id)])
    with pytest.raises(ValueError, match="exactly cover"):
        router.submit(run, _attempt(work, "accepted", bad))
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM model_assessment WHERE committee_run_id=? AND role='red_team'",
                (run,),
            ).fetchone()[0]
            == 0
        )


def test_red_team_remainder_is_exact_arbiter_focus_then_scores(tmp_path):
    store, router, run, pack_hash = _disputed_committee(tmp_path / "arbiter.db")
    red_work = router.get_work(run)
    ids = [item["item_id"] for item in red_work["focus"]["items"]]
    red = _verdict_assessment(
        pack_hash,
        "red_team",
        "provider-red",
        [_verdict(ids[0]), _verdict(ids[1], "unresolved"), _verdict(ids[2])],
    )
    assert router.submit(run, _attempt(red_work, "accepted", red))["state"] == "ARBITER_REQUIRED"
    arbiter_work = router.get_work(run)
    assert [item["item_id"] for item in arbiter_work["focus"]["items"]] == [ids[1]]
    arbiter = _verdict_assessment(
        pack_hash, "arbiter", "provider-arbiter", [_verdict(ids[1], "resolved_for_b")]
    )
    result = router.submit(run, _attempt(arbiter_work, "accepted", arbiter))
    assert result["state"] == "SCORED"
    with store.database.connect(read_only=True) as db:
        resolutions = list(
            db.execute(
                "SELECT role,focus_hash,focus_json FROM dispute_resolution WHERE committee_run_id=? ORDER BY role",
                (run,),
            )
        )
        assert len(resolutions) == 2
        assert json.loads(resolutions[0]["focus_json"])["items"] == arbiter_work["focus"]["items"]
        assert (
            db.execute(
                "SELECT count(*) FROM score_snapshot WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 1
        )


def test_resolving_red_team_verdict_requires_in_pack_evidence_atomically(tmp_path):
    store, router, run, pack_hash = _disputed_committee(tmp_path / "red-no-evidence.db")
    work = router.get_work(run)
    verdicts = [_verdict(item["item_id"]) for item in work["focus"]["items"]]
    verdicts[0]["cited_evidence_ids"] = []
    red = _verdict_assessment(pack_hash, "red_team", "provider-red", verdicts)
    with pytest.raises(AssessmentValidationError, match="resolving verdict must cite"):
        router.submit(run, _attempt(work, "accepted", red))
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute("SELECT count(*) FROM model_assessment WHERE role='red_team'").fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM dispute_resolution").fetchone()[0] == 0
    assert store.current_state(run) == "RED_TEAM_REQUIRED"


def test_resolving_arbiter_verdict_requires_in_pack_evidence_atomically(tmp_path):
    store, router, run, pack_hash = _disputed_committee(tmp_path / "arbiter-no-evidence.db")
    red_work = router.get_work(run)
    red = _verdict_assessment(
        pack_hash,
        "red_team",
        "provider-red",
        [_verdict(item["item_id"], "unresolved") for item in red_work["focus"]["items"]],
    )
    router.submit(run, _attempt(red_work, "accepted", red))
    work = router.get_work(run)
    verdicts = [_verdict(item["item_id"], "resolved_for_b") for item in work["focus"]["items"]]
    verdicts[0]["cited_evidence_ids"] = []
    arbiter = _verdict_assessment(pack_hash, "arbiter", "provider-arbiter", verdicts)
    with pytest.raises(AssessmentValidationError, match="resolving verdict must cite"):
        router.submit(run, _attempt(work, "accepted", arbiter))
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute("SELECT count(*) FROM model_assessment WHERE role='arbiter'").fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT count(*) FROM dispute_resolution WHERE role='arbiter'").fetchone()[0]
            == 0
        )
    assert store.current_state(run) == "ARBITER_REQUIRED"


def test_exact_full_red_team_coverage_resolves_and_scores(tmp_path):
    _, router, run, pack_hash = _disputed_committee(tmp_path / "red-resolved.db")
    work = router.get_work(run)
    verdicts = [_verdict(item["item_id"]) for item in work["focus"]["items"]]
    red = _verdict_assessment(pack_hash, "red_team", "provider-red", verdicts)
    result = router.submit(run, _attempt(work, "accepted", red))
    assert result["state"] == "SCORED" and result["required_work"] == []


def test_unresolved_arbiter_escalates_without_score(tmp_path):
    store, router, run, pack_hash = _disputed_committee(tmp_path / "arbiter-open.db")
    red_work = router.get_work(run)
    red_verdicts = [_verdict(item["item_id"], "unresolved") for item in red_work["focus"]["items"]]
    red = _verdict_assessment(pack_hash, "red_team", "provider-red", red_verdicts)
    router.submit(run, _attempt(red_work, "accepted", red))
    arbiter_work = router.get_work(run)
    arbiter_verdicts = [
        _verdict(item["item_id"], "unresolved") for item in arbiter_work["focus"]["items"]
    ]
    arbiter = _verdict_assessment(pack_hash, "arbiter", "provider-arbiter", arbiter_verdicts)
    assert router.submit(run, _attempt(arbiter_work, "accepted", arbiter))["state"] == "ESCALATE"
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM score_snapshot").fetchone()[0] == 0


def test_arbiter_malformed_exhaustion_escalates(tmp_path):
    _, router, run, pack_hash = _disputed_committee(tmp_path / "arbiter-malformed.db")
    red_work = router.get_work(run)
    red = _verdict_assessment(
        pack_hash,
        "red_team",
        "provider-red",
        [_verdict(item["item_id"], "unresolved") for item in red_work["focus"]["items"]],
    )
    router.submit(run, _attempt(red_work, "accepted", red))
    first = router.get_work(run)
    assert router.submit(run, _attempt(first, "malformed"))["state"] == "ARBITER_REQUIRED"
    second = router.get_work(run)
    assert router.submit(run, _attempt(second, "malformed"))["state"] == "ESCALATE"


def test_attempt_polling_is_idempotent_and_exhaustion_fails_closed(tmp_path):
    store, run, _ = _committee(tmp_path / "unavailable.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    first = router.get_work(run)
    assert router.get_work(run)["work_id"] == first["work_id"]
    assert router.submit(run, _attempt(first, "unavailable"))["state"] == "PENDING_NEUTRALS"
    second = router.get_work(run)
    assert second["attempt_number"] == 2
    result = router.submit(run, _attempt(second, "unavailable"))
    assert result["state"] == "BLOCKED" and result["work"] == []
    with store.database.connect(read_only=True) as db:
        # committee_status also issues B's visible envelope, but polling never debits it.
        assert db.execute("SELECT count(*) FROM committee_work").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM model_call_attempt").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM score_snapshot").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM model_call_attempt").fetchone()[0] <= 8


def test_malformed_twice_escalates_and_never_degrades_to_one_neutral(tmp_path):
    store, run, _ = _committee(tmp_path / "malformed.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    first = router.get_work(run)
    assert router.submit(run, _attempt(first, "malformed"))["state"] == "PENDING_NEUTRALS"
    second = router.get_work(run)
    result = router.submit(run, _attempt(second, "malformed"))
    assert result["state"] == "ESCALATE" and result["accepted_roles"] == []
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM score_snapshot").fetchone()[0] == 0


def test_b_first_same_provider_is_rejected_symmetrically(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "provider-order.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_b", "same"))
    with pytest.raises(ValueError, match="providers must differ"):
        router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_a", "same"))
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM model_assessment WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 1
        )


def test_concurrent_neutrals_cannot_commit_the_same_provider(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "provider-concurrency.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    work = {item["role"]: item for item in router.status(run)["work"]}
    barrier = Barrier(2)
    original = CommitteeRouter._validate_provider_independence

    def synchronized_check(self, run_id, role, provider):
        original(self, run_id, role, provider)
        barrier.wait(timeout=5)

    def submit(role):
        assessment = _valid_assessment(pack_hash, role, "same")
        try:
            return router.submit(run, _attempt(work[role], "accepted", assessment))
        except ValueError as exc:
            return exc

    with patch.object(CommitteeRouter, "_validate_provider_independence", synchronized_check):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ("neutral_analyst_a", "neutral_analyst_b")))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum("neutral providers must differ" in str(result) for result in results) == 1
    with store.database.connect(read_only=True) as db:
        rows = list(
            db.execute(
                "SELECT role,provider FROM model_assessment WHERE committee_run_id=?", (run,)
            )
        )
        assert len(rows) == 1 and rows[0]["provider"] == "same"


def test_accepted_retry_and_telemetry_clone_keep_one_snapshot_identity(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "retry.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    a = _valid_assessment(
        pack_hash, "neutral_analyst_a", "provider-a", [_claim("valuation_vs_history", "bullish")]
    )
    work = router.get_work(run)
    accepted = _attempt(work, "accepted", a)
    first = router.submit(run, accepted)
    identical = router.submit(run, accepted)
    assert identical["assessment_id"] == first["assessment_id"]
    assert identical["attempt_id"] == first["attempt_id"]
    clone = deepcopy(a)
    clone.update(provider="telemetry-provider", model_id="other", model_route="other")
    clone["usage"] = {
        "input_tokens": 99,
        "output_tokens": 7,
        "cached_tokens": 2,
        "source": "SELF_REPORTED",
    }
    clone["cost"] = {"amount": "1.25", "currency": "USD", "source": "SELF_REPORTED"}
    clone_attempt = _attempt(work, "accepted", clone)
    retry = router.submit(run, clone_attempt)
    assert retry["accepted_roles"] == first["accepted_roles"]
    assert retry["assessment_id"] == first["assessment_id"]
    b = _valid_assessment(
        pack_hash, "neutral_analyst_b", "provider-b", [_claim("valuation_vs_history", "bullish")]
    )
    scored = router.submit(run, b)
    assert scored["state"] == "SCORED"
    router.submit(run, clone_attempt)
    with store.database.connect(read_only=True) as db:
        snapshots = list(
            db.execute(
                "SELECT snapshot_id,score_input_hash FROM score_snapshot WHERE committee_run_id=?",
                (run,),
            )
        )
        assert len(snapshots) == 1
        assert (
            db.execute(
                "SELECT count(*) FROM model_assessment WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 2
        )


def test_telemetry_only_full_paths_have_identical_logical_artifacts(tmp_path):
    def execute(path: Path, telemetry: bool) -> tuple[str, str, str]:
        store, run, pack_hash = _committee(path)
        router = CommitteeRouter(store.database)
        router.initialize(run)
        claims = [_claim("valuation_vs_history", "bullish")]
        a = _valid_assessment(pack_hash, "neutral_analyst_a", "provider-a", claims)
        b = _valid_assessment(pack_hash, "neutral_analyst_b", "provider-b", claims)
        if telemetry:
            for index, assessment in enumerate((a, b), start=1):
                assessment.update(model_id=f"other-{index}", model_route=f"route-{index}")
                assessment["usage"] = {
                    "input_tokens": 100 + index,
                    "output_tokens": 10 + index,
                    "cached_tokens": index,
                    "source": "SELF_REPORTED",
                }
                assessment["cost"] = {
                    "amount": f"0.0{index}",
                    "currency": "USD",
                    "source": "SELF_REPORTED",
                }
        router.submit(run, a)
        router.submit(run, b)
        with store.database.connect(read_only=True) as db:
            comparison = db.execute(
                "SELECT comparison_id FROM comparison_report WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            snapshot = db.execute(
                "SELECT snapshot_id,score_input_hash FROM score_snapshot WHERE committee_run_id=?",
                (run,),
            ).fetchone()
        return comparison, snapshot[0], snapshot[1]

    assert execute(tmp_path / "plain.db", False) == execute(tmp_path / "telemetry.db", True)


def test_semantic_snapshot_reuse_completes_and_links_every_run(tmp_path):
    store, first_run, pack_hash = _committee(tmp_path / "snapshot-reuse.db")
    comparator, scoring = store.ensure_registry_rows()
    claims = [_claim("valuation_vs_history", "bullish")]
    a = _valid_assessment(pack_hash, "neutral_analyst_a", "provider-a", claims)
    b = _valid_assessment(pack_hash, "neutral_analyst_b", "provider-b", claims)
    first_router = CommitteeRouter(store.database)
    first_router.initialize(first_run)
    first_router.submit(first_run, a)
    first = first_router.submit(first_run, b)
    second_run = store.create_or_resume_committee_run(
        candidate_id="candidate",
        pack_hash=pack_hash,
        committee_policy_version=2,
        comparator_config_hash=comparator,
        scoring_config_hash=scoring,
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
        assessment_schema_version=1,
    )
    second_router = CommitteeRouter(store.database)
    second_router.initialize(second_run)
    second_router.submit(second_run, a)
    second = second_router.submit(second_run, b)
    assert first["state"] == second["state"] == "SCORED"
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["artifact_id"] == second["artifact_id"] == first["snapshot_id"]
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM score_snapshot").fetchone()[0] == 1
        links = list(
            db.execute(
                "SELECT committee_run_id,artifact_id FROM committee_transition "
                "WHERE to_state='SCORED' ORDER BY committee_run_id"
            )
        )
        assert {row["committee_run_id"] for row in links} == {first_run, second_run}
        assert {row["artifact_id"] for row in links} == {first["snapshot_id"]}


def test_ready_to_score_status_recovers_after_injected_score_failure(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "recover.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    claims = [_claim("valuation_vs_history", "bullish")]
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_a", "provider-a", claims))
    with patch.object(router, "_score", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            router.submit(
                run, _valid_assessment(pack_hash, "neutral_analyst_b", "provider-b", claims)
            )
    assert store.current_state(run) == "READY_TO_SCORE"
    assert router.status(run)["state"] == "SCORED"
    with store.database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM score_snapshot WHERE committee_run_id=?", (run,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("surface", ["status", "get_work", "retry"])
def test_second_neutral_commit_boundary_recovers_once(tmp_path, surface):
    store, run, pack_hash = _committee(tmp_path / f"neutral-recover-{surface}.db")
    router = CommitteeRouter(store.database)
    router.initialize(run)
    claims = [_claim("valuation_vs_history", "bullish")]
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_a", "provider-a", claims))
    second = _valid_assessment(pack_hash, "neutral_analyst_b", "provider-b", claims)
    with patch.object(Comparator, "compare_and_persist", side_effect=RuntimeError("injected")):
        with pytest.raises(RuntimeError, match="injected"):
            router.submit(run, second)
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM model_assessment").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM comparison_report").fetchone()[0] == 0
    assert store.current_state(run) == "PENDING_NEUTRALS"
    if surface == "status":
        recovered = router.status(run)
    elif surface == "get_work":
        assert router.get_work(run) is None
        recovered = router.status(run)
    else:
        recovered = router.submit(run, second)
        assert "assessment_id" in recovered
    assert recovered["state"] == "SCORED" and recovered["artifact_id"]
    with store.database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM comparison_report").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM score_snapshot").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM committee_transition WHERE cause_code='NEUTRALS_COMPARED'"
            ).fetchone()[0]
            == 1
        )


def test_pack_replay_ignores_later_cluster_and_ticker_rows(tmp_path):
    database, candidate_id = _fixture(tmp_path / "pack-replay.db")
    first = EvidencePackBuilder(database).build(candidate_id)
    with database.connect() as db:
        db.execute("INSERT INTO evidence_cluster VALUES (?,?,?)", ("later", "later", "2025-01-01Z"))
        db.execute("INSERT INTO evidence_cluster_member VALUES (?,?)", ("event1", "later"))
        db.execute(
            "INSERT INTO security_identity_event(security_id,event_type,old_value,new_value,event_time,"
            "public_available_time,pat_provenance,ingested_time) VALUES (?,?,?,?,?,?,?,?)",
            (
                "sec",
                "ticker_change",
                "OLD",
                "NEW",
                "2025-01-01Z",
                "2025-01-01Z",
                "source_reported",
                "2025-01-02Z",
            ),
        )
    assert EvidencePackBuilder(database).build(candidate_id) == first


def _identity_pack(tmp_path: Path, as_of: str, events: list[dict[str, object]]):
    database, candidate_id = _fixture(tmp_path, as_of=as_of)
    store = SecurityIdentityStore(database)
    inserted: list[int] = []
    for event in events:
        values = dict(event)
        predecessor = values.pop("supersedes_index", None)
        if predecessor is not None:
            values["supersedes_id"] = inserted[int(predecessor)]
        inserted.append(store.insert(security_id="sec", **values))
    return EvidencePackBuilder(database).build(candidate_id)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    (("2025-03-01", "OLD"), ("2025-07-01", "NEW")),
)
def test_pack_ticker_requires_announcement_and_effective_time(tmp_path, as_of, expected):
    pack = _identity_pack(
        tmp_path / f"effective-{expected}.db",
        as_of,
        [
            {
                "event_type": "baseline",
                "old_value": None,
                "new_value": "OLD",
                "event_time": "2025-01-01",
                "public_available_time": "2025-01-01",
                "pat_provenance": "source_reported",
            },
            {
                "event_type": "ticker_change",
                "old_value": "OLD",
                "new_value": "NEW",
                "event_time": "2025-06-01",
                "public_available_time": "2025-02-01",
                "pat_provenance": "source_reported",
                "supersedes_index": 0,
            },
        ],
    )
    assert pack.body["identity"]["ticker_as_of"] == expected


@pytest.mark.parametrize(
    ("as_of", "expected"),
    (("2025-07-01", "NEW"), ("2025-09-01", "CORRECTED")),
)
def test_pack_ticker_uses_knowable_supersession_version(tmp_path, as_of, expected):
    pack = _identity_pack(
        tmp_path / f"correction-{expected}.db",
        as_of,
        [
            {
                "event_type": "baseline",
                "old_value": None,
                "new_value": "OLD",
                "event_time": "2025-01-01",
                "public_available_time": "2025-01-01",
                "pat_provenance": "source_reported",
            },
            {
                "event_type": "ticker_change",
                "old_value": "OLD",
                "new_value": "NEW",
                "event_time": "2025-06-01",
                "public_available_time": "2025-02-01",
                "pat_provenance": "source_reported",
                "supersedes_index": 0,
            },
            {
                "event_type": "ticker_change",
                "old_value": "OLD",
                "new_value": "CORRECTED",
                "event_time": "2025-06-01",
                "public_available_time": "2025-08-01",
                "pat_provenance": "derived_from_index",
                "supersedes_index": 1,
            },
        ],
    )
    assert pack.body["identity"]["ticker_as_of"] == expected


def test_pack_ticker_without_identity_event_uses_canonical_ticker(tmp_path):
    database, candidate_id = _fixture(tmp_path / "canonical-ticker.db")
    assert (
        EvidencePackBuilder(database).build(candidate_id).body["identity"]["ticker_as_of"] == "TST"
    )


def test_candidate_trends_follow_as_of_not_snapshot_hash(tmp_path):
    database, candidate_id = _fixture(tmp_path / "chronology.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    store = CommitteeStore(database)
    comparator, scoring = store.ensure_registry_rows()
    raw = sqlite3.connect(database.path)
    try:
        for run_id, as_of in (
            ("history-old", "2025-01-01T00:00:00Z"),
            ("history-mid", "2025-01-02T00:00:00Z"),
            ("history-new", "2025-01-03T00:00:00Z"),
        ):
            raw.execute(
                "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,"
                "screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,"
                "input_view_hash,expected_security_count,status,failure_json,started_at,finished_at,"
                "flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    as_of,
                    "u",
                    "[]",
                    f"manifest-{run_id}",
                    "{}",
                    "funnel",
                    None,
                    f"view-{run_id}",
                    1,
                    "COMPLETE",
                    None,
                    as_of,
                    as_of,
                    "[]",
                ),
            )
            raw.execute(
                "INSERT INTO committee_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"committee-{run_id}",
                    candidate_id,
                    run_id,
                    pack.pack_hash,
                    "[]",
                    1,
                    comparator,
                    scoring,
                    "{}",
                    1,
                    as_of,
                ),
            )
        for snapshot_id, run_id, conviction in (
            ("z-old", "history-old", 15),
            ("a-mid", "history-mid", 85),
            ("m-new", "history-new", 40),
        ):
            raw.execute(
                "INSERT INTO score_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    candidate_id,
                    f"committee-{run_id}",
                    scoring,
                    f"input-{snapshot_id}",
                    f"evidence-{snapshot_id}",
                    "[]",
                    "comparison",
                    "[]",
                    "{}",
                    "[]",
                    "{}",
                    0,
                    0,
                    conviction,
                    conviction,
                    0.5,
                    0.5,
                    None,
                    None,
                    None,
                    "STABLE",
                    "EVIDENCE_DRIVEN",
                    None,
                    "[]",
                    f"result-{snapshot_id}",
                    as_of,
                ),
            )
        raw.commit()
    finally:
        raw.close()
    snapshots = Scorer(database).list_candidate(candidate_id)
    assert [item["snapshot_id"] for item in snapshots] == ["z-old", "a-mid", "m-new"]
    assert snapshots[-1]["trend_3"] == 25


def test_prior_snapshot_selection_excludes_future_as_of(tmp_path):
    store, run, pack_hash = _committee(tmp_path / "prior-bound.db")
    raw = sqlite3.connect(store.database.path)
    try:
        raw.execute(
            "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,"
            "screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,"
            "input_view_hash,expected_security_count,status,failure_json,started_at,finished_at,"
            "flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "future-run",
                "2026-01-01T00:00:00Z",
                "u",
                "[]",
                "future-manifest",
                "{}",
                "funnel",
                None,
                "future-view",
                1,
                "COMPLETE",
                None,
                "2026-01-01Z",
                "2026-01-01Z",
                "[]",
            ),
        )
        raw.execute(
            "INSERT INTO committee_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "future-committee",
                "candidate",
                "future-run",
                pack_hash,
                "[]",
                1,
                ComparatorSpec().config_hash,
                ScoringSpec().config_hash,
                "{}",
                1,
                "2026-01-01Z",
            ),
        )
        raw.execute(
            "INSERT INTO score_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "future-snapshot",
                "candidate",
                "future-committee",
                ScoringSpec().config_hash,
                "future-input",
                "future-evidence",
                "[]",
                "comparison",
                "[]",
                "{}",
                "[]",
                "{}",
                0,
                0,
                90,
                90,
                1.0,
                1.0,
                None,
                None,
                None,
                "INITIAL",
                "INITIAL",
                None,
                "[]",
                "future-result",
                "2026-01-01Z",
            ),
        )
        raw.commit()
    finally:
        raw.close()
    router = CommitteeRouter(store.database)
    router.initialize(run)
    claims = [_claim("valuation_vs_history", "bullish")]
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_a", "provider-a", claims))
    router.submit(run, _valid_assessment(pack_hash, "neutral_analyst_b", "provider-b", claims))
    with store.database.connect(read_only=True) as db:
        current = db.execute(
            "SELECT prior_snapshot_id FROM score_snapshot WHERE committee_run_id=?", (run,)
        ).fetchone()
    assert current[0] is None

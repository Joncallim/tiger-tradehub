"""#67 regressions: scoring lineage vs bounded committee view.

These tests pin the four claims #67 makes:

1. a candidate whose momentum lineage makes pack v1 too large still gets a
   complete scoring artifact and a bounded model view;
2. scoring is byte-for-byte identical whether it reads the legacy pack or the
   lineage (golden equivalence);
3. model-facing view bounds cannot move scoring, ``scored_evidence_hash`` or
   trajectory semantics;
4. the view is honest: aggregates are labelled, omissions are counted, and an
   omitted observation cannot be cited by a model.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tradehub_research.committee import bounds as bounds_module
from tradehub_research.committee import view as view_module
from tradehub_research.committee.assessment import AssessmentValidationError, _id_list
from tradehub_research.committee.frozen_inputs import PackBuildError
from tradehub_research.committee.lineage import ScoringLineageBuilder
from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.scoring import (
    Scorer,
    _semantic_screen_hashes,
    score_screens,
)
from tradehub_research.committee.store import CommitteeStore, ScoringSpec
from tradehub_research.committee.view import VIEW_PACK_SPEC_VERSION, CommitteeViewBuilder
from tradehub_research.db import ResearchDB
from tradehub_research.screens import ScreenResult, ScreenSpec, canonical_json

# ruff: noqa: E501 -- fixture SQL mirrors complete immutable table layouts.
AS_OF = "2025-06-30T00:00:00Z"
EQUIV_FIELDS = (
    "family_contributions",
    "underlying_groups",
    "penalties",
    "base_evidence",
    "confluence_bonus",
    "raw_score",
    "conviction",
    "data_quality",
    "scored_evidence_hash",
)


def _bar_observations(count: int) -> list[dict[str, object]]:
    return [
        {
            "evidence_id": f"bar{index:04d}",
            "role": "price_bar",
            "session_date": f"2024-{index % 12 + 1:02d}-{index % 28 + 1:02d}",
            "unit": "usd_per_share",
            "value": 10.0 + index,
        }
        for index in range(count)
    ]


def _fixture(
    path: Path,
    *,
    bars: int = 12,
    series_length: int = 50,
    late_evidence: bool = False,
) -> tuple[ResearchDB, str]:
    """Seed one candidate with a momentum series plus a small valuation screen."""
    database = ResearchDB(path)
    database.migrate()
    momentum_spec = ScreenSpec("momentum_confirmation", "mom", 1, 1, {}, [], "test")
    valuation_spec = ScreenSpec("valuation", "value", 1, 1, {}, [], "test")
    observations = _bar_observations(series_length)
    momentum_features = {
        "adv_20d": {"sources": observations, "unit": "usd", "value": 63414310.24},
        "eligible_bar_count": {
            "sources": observations,
            "unit": "count",
            "value": float(series_length),
        },
    }
    momentum = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=momentum_spec.config_hash,
        raw_features=momentum_features,
        evidence_ids=[f"bar{index:04d}" for index in range(bars)],
        reason_codes=[],
        sufficient_data=True,
        passed=True,
        confidence=0.7,
        data_quality=0.5,
    )
    valuation = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=valuation_spec.config_hash,
        raw_features={"note": "ok"},
        evidence_ids=["val1", "val2"],
        reason_codes=[],
        sufficient_data=True,
        passed=True,
        confidence=0.9,
        data_quality=1.0,
    )
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("sec", "TST", "NYSE", "Test", "Tech", None, "SUPPORTED", "2024-01-01Z", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("src", "price", 1, None, "source_reported"),
        )
        for index in range(bars):
            evidence_id = f"bar{index:04d}"
            public_available_time = "2024-06-02T00:00:00Z"
            ingested_at = "2024-06-03T00:00:00Z"
            if late_evidence and index == 0:
                # A point-in-time violation: available AFTER the run cutoff.
                public_available_time = "2026-01-01T00:00:00Z"
                ingested_at = "2026-01-02T00:00:00Z"
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json({"record_type": "price_bar", "value": 10.0 + index}),
                    1.0,
                    None,
                    0,
                    evidence_id + "hash",
                    evidence_id,
                    "2024-06-01T00:00:00Z",
                    public_available_time,
                    "source_reported",
                    ingested_at,
                ),
            )
        for evidence_id in ("val1", "val2"):
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json({"record_type": "xbrl_fact", "accession": "acc", "value": 1}),
                    1.0,
                    None,
                    0,
                    evidence_id + "hash",
                    evidence_id,
                    "2024-06-01T00:00:00Z",
                    "2024-06-02T00:00:00Z",
                    "source_reported",
                    "2024-06-03T00:00:00Z",
                ),
            )
        for spec in (momentum_spec, valuation_spec):
            db.execute(
                "INSERT INTO screen_definition VALUES (?,?,?,?,?,?)",
                (
                    spec.config_hash,
                    spec.family,
                    spec.screen_id,
                    spec.screen_version,
                    spec.canonical_json(),
                    "2024-01-01Z",
                ),
            )
        db.execute(
            "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,input_view_hash,expected_security_count,status,failure_json,started_at,finished_at,flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run",
                AS_OF,
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
                "2024-01-01Z",
                None,
                "[]",
            ),
        )
        for result in (momentum, valuation):
            db.execute(
                "INSERT INTO screen_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result.screen_result_id,
                    "run",
                    "sec",
                    result.config_hash,
                    canonical_json(result.raw_features),
                    canonical_json(result.evidence_ids),
                    "[]",
                    1,
                    1,
                    result.confidence,
                    result.data_quality,
                    result.result_hash,
                    "2024-06-04Z",
                ),
            )
        db.execute(
            "UPDATE pipeline_run SET status='COMPLETE',finished_at='2024-06-05Z' WHERE run_id='run'"
        )
        db.execute(
            "INSERT INTO candidate VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate",
                "run",
                "sec",
                1,
                "[]",
                canonical_json([momentum.screen_result_id, valuation.screen_result_id]),
                "{}",
                0,
                None,
                None,
                None,
                "2024-06-05Z",
            ),
        )
    return database, "candidate"


def test_lineage_and_view_materialize_where_pack_v1_is_blocked(tmp_path):
    """The 34/34 mechanism: a large passing series no longer blocks the candidate."""
    database, candidate_id = _fixture(tmp_path / "blocked.db", bars=300, series_length=300)
    with pytest.raises(PackBuildError) as failure:
        EvidencePackBuilder(database).build(candidate_id)
    assert failure.value.code == "PACK_TOO_LARGE"

    lineage = ScoringLineageBuilder(database).build(candidate_id)
    assert lineage.body["counts"]["evidence_identity"] == 302
    assert lineage.body["counts"]["frozen_evidence"] == 302
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    assert view.body["bounds"]["view_bytes"] <= bounds_module.MAX_BODY_BYTES
    assert view.body["evidence_omitted"]["interpretive_omitted"] == 0
    assert view.lineage_hash == lineage.lineage_hash


def test_golden_equivalence_v1_pack_vs_lineage(tmp_path):
    """Same frozen inputs, same scoring: values and identity hashes must match."""
    database, candidate_id = _fixture(tmp_path / "equivalence.db", bars=12, series_length=50)
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    spec = ScoringSpec().as_dict()
    legacy = score_screens(pack.body["screens"], pack.body["evidence"], spec)
    current = score_screens(lineage.screens, lineage.evidence_identity, spec)
    for field in EQUIV_FIELDS:
        assert legacy[field] == current[field], field
    assert _semantic_screen_hashes(pack.body["screens"]) == _semantic_screen_hashes(lineage.screens)


def test_view_bounds_cannot_affect_scoring(tmp_path):
    """Model-facing limits are inert with respect to scoring identity."""
    reference_db, candidate_id = _fixture(tmp_path / "reference.db", bars=300, series_length=300)
    reference = ScoringLineageBuilder(reference_db).build(candidate_id)
    reference_result = score_screens(
        reference.screens, reference.evidence_identity, ScoringSpec().as_dict()
    )
    reference_view = CommitteeViewBuilder(reference_db).build(candidate_id, lineage=reference)

    squeezed_db, _ = _fixture(tmp_path / "squeezed.db", bars=300, series_length=300)
    squeezed_lineage = ScoringLineageBuilder(squeezed_db).build(candidate_id)
    original_rows = view_module.MAX_VIEW_EVIDENCE_ROWS
    original_bytes = view_module.MAX_BODY_BYTES
    try:
        view_module.MAX_VIEW_EVIDENCE_ROWS = 1
        view_module.MAX_BODY_BYTES = 20_000
        squeezed_view = CommitteeViewBuilder(squeezed_db).build(
            candidate_id, lineage=squeezed_lineage
        )
    finally:
        view_module.MAX_VIEW_EVIDENCE_ROWS = original_rows
        view_module.MAX_BODY_BYTES = original_bytes

    assert squeezed_view.pack_hash != reference_view.pack_hash
    assert squeezed_view.body["bounds"]["evidence_rows"] == 1
    assert (
        squeezed_view.body["bounds"]["evidence_omitted"]
        > reference_view.body["evidence_omitted"]["interpretive_omitted"]
    )
    assert squeezed_view.body["bounds"]["view_bytes"] <= 20_000
    # The scoring artifacts are untouched by the squeeze.
    assert squeezed_lineage.lineage_hash == reference.lineage_hash
    assert (
        score_screens(
            squeezed_lineage.screens, squeezed_lineage.evidence_identity, ScoringSpec().as_dict()
        )
        == reference_result
    )


def test_aggregated_observations_cannot_be_cited_but_representatives_can(tmp_path):
    """The model citation firewall sees the view, not the lineage."""
    database, candidate_id = _fixture(tmp_path / "citations.db", bars=300, series_length=300)
    view = CommitteeViewBuilder(database).build(candidate_id)
    body = view.body
    presented = {row["evidence_id"] for row in body["evidence"]}
    aggregates = [
        aggregate for screen in body["screens"] for aggregate in screen["series_aggregates"]
    ]
    assert aggregates
    representative_ids = {
        evidence_id
        for aggregate in aggregates
        for evidence_id in aggregate["representative_evidence_ids"]
    }
    assert representative_ids <= presented, "aggregate representatives must be citable"
    omitted = [f"bar{index:04d}" for index in range(300) if f"bar{index:04d}" not in presented]
    assert omitted, "this fixture must omit individual observations"
    assert _id_list([sorted(representative_ids)[0]], "cited", presented)
    with pytest.raises(AssessmentValidationError):
        _id_list([omitted[0]], "cited", presented)


def test_view_report_is_honest_about_aggregation_and_omission(tmp_path):
    database, candidate_id = _fixture(tmp_path / "honesty.db", bars=300, series_length=300)
    body = CommitteeViewBuilder(database).build(candidate_id).body
    assert body["representation"] == view_module.REPRESENTATION
    assert body["model_honesty"]["aggregated_series_present"] is True
    assert body["model_honesty"]["citation_scope"] == "evidence rows presented in this view"
    # The honesty contract must be machine-readable, not only prose: these flags
    # are what a reviewer (or an auditor) checks a model's claims against.
    assert body["model_honesty"]["aggregate_fields_are_code_computed"] is True
    assert body["model_honesty"]["omitted_observations_are_not_missing_data"] is True
    assert body["model_honesty"]["lineage_set_hash_is_an_identity_not_market_evidence"] is True
    assert body["evidence_omitted"]["omission_semantics"] == (
        "representation_compaction_not_data_quality"
    )
    assert body["series_representation"]["omission_semantics"] == (
        "representation_compaction_not_data_quality"
    )
    momentum = next(
        entry for entry in body["screens"] if entry["family"] == "momentum_confirmation"
    )
    assert momentum["representation"] == "DETERMINISTIC_AGGREGATE"
    assert all(
        aggregate["observations_omitted"]
        == aggregate["observation_count"] - aggregate["observations_presented"]
        for aggregate in momentum["series_aggregates"]
    )
    assert all(aggregate["lineage_set_hash"] for aggregate in momentum["series_aggregates"])
    assert body["series_representation"]["distinct_observations"] == 300
    assert body["lineage"]["full_lineage_available"] is True
    assert len(body["lineage"]["lineage_hash"]) == 64
    # Lineage content itself must never be embedded in the model-facing artifact.
    assert "evidence_identity" not in json.dumps(body)[:200]
    omitted = body["evidence_omitted"]
    assert omitted["interpretive_omitted"] == 0
    assert (
        omitted["series_observations_omitted"] == 300 - omitted["series_representatives_presented"]
    )


def test_prior_legacy_run_and_lineage_run_resolve_to_equivalent_semantic_identity(tmp_path):
    """A representation change must not manufacture SCREEN_METHODOLOGY_CHANGE."""
    database, candidate_id = _fixture(tmp_path / "trajectory.db", bars=12, series_length=50)
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()

    legacy_run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=pack.pack_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
        assessment_schema_version=1,
    )
    lineage_run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=view.pack_hash,
        lineage_hash=lineage.lineage_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
        assessment_schema_version=1,
    )
    assert legacy_run != lineage_run
    with database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT lineage_hash FROM committee_run_lineage WHERE committee_run_id=?",
                (lineage_run,),
            ).fetchone()[0]
            == lineage.lineage_hash
        )
        assert (
            db.execute(
                "SELECT lineage_hash FROM committee_run_lineage WHERE committee_run_id=?",
                (legacy_run,),
            ).fetchone()
            is None
        )
        current = db.execute(
            "SELECT * FROM committee_run WHERE committee_run_id=?", (lineage_run,)
        ).fetchone()
        prior = db.execute(
            "SELECT * FROM committee_run WHERE committee_run_id=?", (legacy_run,)
        ).fetchone()
        old_screens, old_evidence, old_kind = Scorer._scoring_artifact(db, prior)
        new_screens, new_evidence, new_kind = Scorer._scoring_artifact(db, current)
    assert old_kind.startswith("legacy-pack-v1:")
    assert new_kind.startswith("lineage:")
    assert _semantic_screen_hashes(old_screens) == _semantic_screen_hashes(new_screens)
    result = score_screens(new_screens, new_evidence, ScoringSpec().as_dict())
    assert score_screens(old_screens, old_evidence, ScoringSpec().as_dict()) == result

    with database.connect(read_only=True) as db:
        cause, label, delta, _material = Scorer(database)._trajectory(
            db,
            {
                "committee_run_id": legacy_run,
                "scoring_config_hash": scoring_hash,
                "scored_evidence_hash": result["scored_evidence_hash"],
                "conviction": result["conviction"],
            },
            {"scoring_config_hash": scoring_hash},
            result,
            _semantic_screen_hashes(new_screens),
            {},
        )
    assert cause == "MODEL_REASSESSMENT"
    assert label == "STABLE"
    assert delta == 0


def test_committee_run_pinned_to_a_view_without_lineage_refuses_to_score(tmp_path):
    database, candidate_id = _fixture(tmp_path / "guard.db", bars=12, series_length=50)
    view = CommitteeViewBuilder(database).build(candidate_id)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    with database.connect() as db:
        db.execute(
            "INSERT INTO committee_run(committee_run_id,candidate_id,pipeline_run_id,pack_hash,"
            "role_set_json,committee_policy_version,comparator_config_hash,scoring_config_hash,"
            "prompt_versions_json,assessment_schema_version,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "orphan",
                candidate_id,
                "run",
                view.pack_hash,
                canonical_json(["neutral_analyst_a"]),
                1,
                comparator_hash,
                scoring_hash,
                canonical_json({"neutral": "v1"}),
                1,
                "2024-06-05Z",
            ),
        )
        row = db.execute("SELECT * FROM committee_run WHERE committee_run_id='orphan'").fetchone()
        with pytest.raises(ValueError, match="must never be a scoring input"):
            Scorer._scoring_artifact(db, row)


def test_loader_gates_apply_to_both_artifacts(tmp_path):
    """PIT/provenance gates are shared, so neither artifact can be softer."""
    database, candidate_id = _fixture(
        tmp_path / "pit.db", bars=12, series_length=50, late_evidence=True
    )
    with pytest.raises(PackBuildError) as lineage_failure:
        ScoringLineageBuilder(database).build(candidate_id)
    assert lineage_failure.value.code == "EVIDENCE_NOT_POINT_IN_TIME"
    with pytest.raises(PackBuildError) as pack_failure:
        EvidencePackBuilder(database).build(candidate_id)
    assert pack_failure.value.code == "EVIDENCE_NOT_POINT_IN_TIME"
    with pytest.raises(PackBuildError) as view_failure:
        CommitteeViewBuilder(database).build(candidate_id)
    assert view_failure.value.code == "EVIDENCE_NOT_POINT_IN_TIME"


def test_view_is_memoized_and_immutable(tmp_path):
    database, candidate_id = _fixture(tmp_path / "memo.db", bars=12, series_length=50)
    first = CommitteeViewBuilder(database).build(candidate_id)
    second = CommitteeViewBuilder(database).build(candidate_id)
    assert first.pack_hash == second.pack_hash
    with database.connect(read_only=True) as db:
        rows = db.execute(
            "SELECT count(*) FROM evidence_pack WHERE candidate_id=? AND pack_spec_version=?",
            (candidate_id, VIEW_PACK_SPEC_VERSION),
        ).fetchone()[0]
    assert rows == 1
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM evidence_pack WHERE candidate_id=?", (candidate_id,))

"""#67 review-finding P1 regression: the methodology hash must not inherit a
model-facing row cap.

Before the fix, ``_semantic_screen_hashes`` hashed each screen's evidence ids
verbatim. Pack v1 stored those ids *intersected with its 256-row selection*,
while the scoring lineage keeps the complete set -- so for any candidate whose
frozen evidence exceeded 256 rows while passing evidence stayed under it (v1
still built and scored), a re-score under #67 recomputed a different
``semantic_screen_hash`` and misclassified a pure representation change as
``SCREEN_METHODOLOGY_CHANGE``.

The fixture below is exactly that boundary: 300 frozen bar ids (momentum does
not pass) plus 20 passing valuation ids.
"""

from __future__ import annotations

from pathlib import Path

from tradehub_research.committee.bounds import MAX_EVIDENCE_ROWS
from tradehub_research.committee.lineage import ScoringLineageBuilder
from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.scoring import (
    Scorer,
    _semantic_screen_hashes,
    methodology_evidence_projection,
    score_screens,
    semantic_screen_hash,
)
from tradehub_research.committee.store import CommitteeStore, ScoringSpec
from tradehub_research.committee.view import CommitteeViewBuilder
from tradehub_research.db import ResearchDB
from tradehub_research.screens import ScreenResult, ScreenSpec, canonical_json

# ruff: noqa: E501 -- fixture SQL mirrors complete immutable table layouts.
AS_OF = "2025-06-30T00:00:00Z"
FROZEN_BARS = 300
PASSING_VALUATIONS = 20


def _fixture(path: Path) -> tuple[ResearchDB, str]:
    database = ResearchDB(path)
    database.migrate()
    momentum_spec = ScreenSpec("momentum_confirmation", "mom", 1, 1, {}, [], "test")
    valuation_spec = ScreenSpec("valuation", "value", 1, 1, {}, [], "test")
    momentum = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=momentum_spec.config_hash,
        raw_features={
            "adv_20d": {
                "sources": [
                    {"evidence_id": f"bar{index:04d}", "role": "price_bar", "value": 1.0}
                    for index in range(5)
                ],
                "unit": "usd",
                "value": 1.0,
            }
        },
        evidence_ids=[f"bar{index:04d}" for index in range(FROZEN_BARS)],
        reason_codes=[],
        sufficient_data=True,
        passed=False,
        confidence=0.4,
        data_quality=0.4,
    )
    valuation = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=valuation_spec.config_hash,
        raw_features={"note": "ok"},
        evidence_ids=[f"val{index:03d}" for index in range(PASSING_VALUATIONS)],
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
        for index in range(FROZEN_BARS):
            evidence_id = f"bar{index:04d}"
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json({"record_type": "price_bar", "value": 1.0}),
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
        for index in range(PASSING_VALUATIONS):
            evidence_id = f"val{index:03d}"
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
                    1 if result is valuation else 0,
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


def test_v1_pack_still_builds_at_the_row_cap_boundary(tmp_path):
    """The boundary must actually be exercised: v1 builds, and it truncated."""
    database, candidate_id = _fixture(tmp_path / "boundary.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    momentum = next(
        screen for screen in pack.body["screens"] if screen["family"] == "momentum_confirmation"
    )
    assert len(pack.body["evidence"]) == MAX_EVIDENCE_ROWS
    # 20 passing valuation ids come first, so momentum keeps 236 of its 300.
    assert len(momentum["evidence_ids"]) == MAX_EVIDENCE_ROWS - PASSING_VALUATIONS
    assert pack.body["bounds"]["truncations"], "the row cap must have bitten"


def test_methodology_identity_is_not_influenced_by_the_row_cap(tmp_path):
    database, candidate_id = _fixture(tmp_path / "identity.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    momentum_pack = next(
        screen for screen in pack.body["screens"] if screen["family"] == "momentum_confirmation"
    )
    momentum_lineage = next(
        screen for screen in lineage.screens if screen["family"] == "momentum_confirmation"
    )
    # Artifact-level divergence is real and deliberate (scoring needs the full set)...
    assert len(momentum_lineage["evidence_ids"]) == FROZEN_BARS
    assert len(momentum_pack["evidence_ids"]) < len(momentum_lineage["evidence_ids"])
    # ...and the raw payloads hash differently, so the projection is load-bearing.
    assert semantic_screen_hash(momentum_pack) != semantic_screen_hash(momentum_lineage)
    # ...but the methodology identity is identical.
    assert _semantic_screen_hashes(pack.body["screens"]) == _semantic_screen_hashes(lineage.screens)
    # The projection is idempotent on a legacy artifact.
    legacy_projection = methodology_evidence_projection(pack.body["screens"])
    assert all(
        legacy_projection[index] == list(screen["evidence_ids"])
        for index, screen in enumerate(pack.body["screens"])
    )


def test_representation_change_alone_is_model_reassessment_not_methodology_change(tmp_path):
    """The reviewer's exact failure scenario, end to end."""
    database, candidate_id = _fixture(tmp_path / "trajectory.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    prior_run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=pack.pack_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
        assessment_schema_version=1,
    )
    current_run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=view.pack_hash,
        lineage_hash=lineage.lineage_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v2", "red_team": "v2", "arbiter": "v2"},
        assessment_schema_version=1,
    )
    assert prior_run != current_run
    spec = ScoringSpec().as_dict()
    result = score_screens(lineage.screens, lineage.evidence_identity, spec)
    # Scoring values and identity are unchanged between the two representations.
    legacy_result = score_screens(pack.body["screens"], pack.body["evidence"], spec)
    assert legacy_result["scored_evidence_hash"] == result["scored_evidence_hash"]
    assert legacy_result["conviction"] == result["conviction"]
    with database.connect(read_only=True) as db:
        cause, label, delta, _material = Scorer(database)._trajectory(
            db,
            {
                "committee_run_id": prior_run,
                "scoring_config_hash": scoring_hash,
                "scored_evidence_hash": result["scored_evidence_hash"],
                "conviction": result["conviction"],
            },
            {"scoring_config_hash": scoring_hash},
            result,
            _semantic_screen_hashes(lineage.screens),
            {},
        )
    assert cause == "MODEL_REASSESSMENT", (
        "a representation change must not manufacture SCREEN_METHODOLOGY_CHANGE"
    )
    assert label == "STABLE"
    assert delta == 0

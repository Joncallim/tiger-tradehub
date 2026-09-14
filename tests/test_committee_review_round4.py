"""#67 review round 4 regressions.

Round 4 verified the round-3 *sampling* fix as real but found the trim never
reached the copy that ships, plus four more defects. This module pins:

1. the shipped ``raw_features`` aggregate copy is trimmed to the admitted rows,
   and every screen's ``evidence_ids`` is reconciled with admission, whenever
   admission declines anyone (row cap or byte budget binding);
2. confluence-group labels are anchored to the historical (v1) selection, so a
   component merge caused by rows outside that selection cannot move
   ``scored_evidence_hash`` and manufacture ``EVIDENCE_DRIVEN``;
3. a foreign lineage object is refused;
4. series omission counts separate "frozen but compacted" from "referenced but
   never frozen";
5. probe rows do not double-record truncation receipts.
"""

from __future__ import annotations

import dataclasses

import pytest
from test_committee_identity_migration import (  # noqa: E402 - sibling test fixture
    _fixture as identity_fixture,
)
from test_committee_methodology_projection import (  # noqa: E402 - sibling test fixture
    _fixture as methodology_fixture,
)

from tradehub_research.committee import view as view_module
from tradehub_research.committee.lineage import ScoringLineageBuilder
from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.scoring import (
    _semantic_screen_hashes,
    classify_trajectory,
    score_screens,
)
from tradehub_research.committee.store import ScoringSpec
from tradehub_research.committee.view import CommitteeViewBuilder, resolve_aggregate
from tradehub_research.db import ResearchDB
from tradehub_research.screen_store import DeterminismError, ScreenResult, ScreenSpec
from tradehub_research.screens import canonical_json

AS_OF = "2025-06-30T00:00:00Z"


def _assert_shipped_copies_agree(view: dict) -> None:
    presented = {row["evidence_id"] for row in view["evidence"]}
    aggregates = [entry for screen in view["screens"] for entry in screen["series_aggregates"]]
    assert aggregates
    for screen in view["screens"]:
        assert set(screen["evidence_ids"]) <= presented, "screen advertises an unadmitted id"
        assert screen["evidence_ids_omitted"] == screen["evidence_id_count"] - len(
            screen["evidence_ids"]
        )
        for entry in screen["series_aggregates"]:
            shipped = resolve_aggregate(screen["raw_features"], entry["path"])
            advertised = set(shipped["representative_evidence_ids"])
            assert advertised <= presented, "shipped aggregate advertises an unadmitted id"
            assert shipped["observations_presented"] == len(advertised)
            assert shipped["observations_omitted"] == (
                shipped["observation_count"] - len(advertised)
            )
            # The summary copy is derived from the shipped copy and must match it.
            assert set(entry["representative_evidence_ids"]) == advertised
            assert shipped["observations_presented"] == entry["observations_presented"]
            assert shipped["observations_omitted"] == entry["observations_omitted"]
            for observation in shipped["representative_observations"]:
                assert observation["evidence_id"] in presented


def test_shipped_aggregate_copy_is_trimmed_when_admission_declines(tmp_path, monkeypatch):
    """Review finding P1/P2 (round 4): the trim must reach the shipped copy."""
    database, candidate_id = methodology_fixture(tmp_path / "trim.db")
    # Force admission to decline representatives: the row cap binds.
    monkeypatch.setattr(view_module, "MAX_VIEW_EVIDENCE_ROWS", 3)
    view = CommitteeViewBuilder(database).build(candidate_id).body
    _assert_shipped_copies_agree(view)
    # The cap really did bind, otherwise the assertion above is vacuous.
    assert view["evidence_omitted"]["series_observations_omitted"] > 0


def test_byte_budget_binding_also_trims_the_shipped_copy(tmp_path, monkeypatch):
    """The byte budget is the second way admission declines a representative."""
    database, candidate_id = methodology_fixture(tmp_path / "bytes.db")
    monkeypatch.setattr(view_module, "MAX_BODY_BYTES", 12_000)
    view = CommitteeViewBuilder(database).build(candidate_id).body
    _assert_shipped_copies_agree(view)
    assert view["evidence_omitted"]["series_observations_omitted"] > 0


def test_probe_rows_do_not_double_record_truncations(tmp_path):
    """Review finding P3 (round 4): truncation receipts must not be inflated."""
    database, candidate_id = methodology_fixture(tmp_path / "truncations.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    receipts = view["bounds"]["truncations"]
    seen = [(entry["kind"], entry.get("path")) for entry in receipts]
    assert len(seen) == len(set(seen)), "a probe row recorded a receipt twice"


def test_series_omission_counts_separate_not_frozen(tmp_path):
    """Review finding P3 (round 4): 'referenced' is not 'retained in lineage'."""
    database, candidate_id = identity_fixture(tmp_path / "counts.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    aggregates = [entry for screen in view["screens"] for entry in screen["series_aggregates"]]
    compacted = sum(entry["observations_compacted"] for entry in aggregates)
    not_frozen = sum(entry["observations_not_frozen"] for entry in aggregates)
    assert not_frozen > 0, "fixture must reference observations that are not frozen evidence"
    assert view["evidence_omitted"]["series_references_not_frozen"] == not_frozen
    total_references = sum(entry["observation_count"] for entry in aggregates)
    assert (
        compacted + not_frozen + sum(entry["observations_presented"] for entry in aggregates)
        == total_references
    )
    semantics = view["evidence_omitted"]["semantics"]
    assert semantics["series_observations"].endswith("retained_in_lineage")
    assert (
        "not part of this candidate's frozen" in semantics["series_references_outside_frozen_set"]
    )


def test_foreign_lineage_is_refused(tmp_path):
    """Review finding P3 (round 4): a view must not embed another candidate's lineage."""
    database, candidate_id = identity_fixture(tmp_path / "owner.db")
    other_db, other_candidate = identity_fixture(tmp_path / "owner2.db", value_base=99.0)
    foreign = ScoringLineageBuilder(other_db).build(other_candidate)
    foreign = dataclasses.replace(
        foreign,
        body={**foreign.body, "candidate": {"candidate_id": "someone-else", "security_id": "sec"}},
    )
    with pytest.raises(DeterminismError, match="different candidate"):
        CommitteeViewBuilder(database).build(candidate_id, lineage=foreign)


def _cluster_fixture(path):
    """Two non-xbrl components inside the historical selection, merged afterwards.

    The historical (v1) selection is the first 256 passing-first ids: 236 passing
    bars plus 10 passing xbrl rows. The passing bars form two cluster components
    inside that selection, and one bar *outside* it shares both clusters -- so a
    whole-set derivation merges them and would relabel already-scored rows.
    """
    database = ResearchDB(path)
    database.migrate()
    bar_ids = [f"bar{index:04d}" for index in range(300)]
    val_ids = [f"val{index:03d}" for index in range(10)]
    passing_bars = bar_ids[:236]
    outside_bars = bar_ids[236:]
    observations = [
        {
            "evidence_id": evidence_id,
            "role": "price_bar",
            "session_date": "2024-06-01",
            "unit": "usd_per_share",
            "value": 1.0,
        }
        for evidence_id in bar_ids
    ]
    momentum_spec = ScreenSpec("momentum_confirmation", "mom", 1, 1, {}, [], "test")
    quality_spec = ScreenSpec("quality", "qual", 1, 1, {}, [], "test")
    valuation_spec = ScreenSpec("valuation", "value", 1, 1, {}, [], "test")
    momentum = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=momentum_spec.config_hash,
        raw_features={"adv_20d": {"sources": observations, "unit": "ratio", "value": 1.0}},
        evidence_ids=passing_bars,
        reason_codes=[],
        sufficient_data=True,
        passed=True,
        confidence=0.7,
        data_quality=0.5,
    )
    quality = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=quality_spec.config_hash,
        raw_features={"coverage": {"sources": observations, "unit": "count", "value": 300.0}},
        evidence_ids=outside_bars,
        reason_codes=[],
        sufficient_data=True,
        passed=False,
        confidence=0.3,
        data_quality=0.4,
    )
    valuation = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=valuation_spec.config_hash,
        raw_features={"note": "ok"},
        evidence_ids=val_ids,
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
        for evidence_id in bar_ids:
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    "{}",
                    1.0,
                    None,
                    0,
                    evidence_id + "hash",
                    evidence_id,
                    "2024-06-01Z",
                    "2024-06-02Z",
                    "source_reported",
                    "2024-06-03Z",
                ),
            )
        for evidence_id in val_ids:
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json({"record_type": "xbrl_fact", "accession": "acc"}),
                    1.0,
                    None,
                    0,
                    evidence_id + "hash",
                    evidence_id,
                    "2024-06-01Z",
                    "2024-06-02Z",
                    "source_reported",
                    "2024-06-03Z",
                ),
            )
        # c1: the first half of the passing bars plus the bridging outside bar.
        # c2: the second half of the passing bars plus the bridging outside bar.
        # c3: the remaining outside bars.
        bridging = outside_bars[0]
        for cluster_id, members in (
            ("c1", passing_bars[:118] + [bridging]),
            ("c2", passing_bars[118:] + [bridging]),
            ("c3", outside_bars[1:]),
        ):
            db.execute(
                "INSERT INTO evidence_cluster VALUES (?,?,?)",
                (cluster_id, cluster_id, AS_OF),
            )
            for evidence_id in members:
                db.execute(
                    "INSERT INTO evidence_cluster_member VALUES (?,?)",
                    (evidence_id, cluster_id),
                )
        for spec in (momentum_spec, quality_spec, valuation_spec):
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
            "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,"
            "screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,"
            "input_view_hash,expected_security_count,status,failure_json,started_at,"
            "finished_at,flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        for result in (momentum, quality, valuation):
            db.execute(
                "INSERT INTO screen_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    result.screen_result_id,
                    "run",
                    "sec",
                    result.config_hash,
                    canonical_json(result.raw_features),
                    canonical_json(result.evidence_ids),
                    canonical_json(result.reason_codes),
                    int(result.sufficient_data),
                    int(result.passed),
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
                "cand",
                "run",
                "sec",
                1,
                "[]",
                canonical_json(
                    [
                        momentum.screen_result_id,
                        quality.screen_result_id,
                        valuation.screen_result_id,
                    ]
                ),
                "{}",
                0,
                None,
                None,
                None,
                "2024-06-05Z",
            ),
        )
    return database, "cand"


def test_group_labels_are_anchored_to_the_historical_selection(tmp_path):
    """Review finding P2 (round 4): a merge caused by rows outside the historical
    selection must not change the labels of rows inside it."""
    database, candidate_id = _cluster_fixture(tmp_path / "groups.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    pack_labels = {row["evidence_id"]: row["underlying_group"] for row in pack.body["evidence"]}
    selected = {
        row["evidence_id"] for row in pack.body["evidence"]
    }  # the historical selection, by construction
    lineage_labels = {
        row["evidence_id"]: row["underlying_group"] for row in lineage.evidence_identity
    }
    assert selected <= set(lineage_labels)
    for evidence_id in sorted(selected):
        assert lineage_labels[evidence_id] == pack_labels[evidence_id], (
            f"label drift for {evidence_id} inside the historical selection"
        )
    # The scenario is real: rows outside the selection exist and carry labels.
    outside = sorted(set(lineage_labels) - selected)
    assert outside, "fixture must contain rows outside the historical selection"
    assert all(lineage_labels[evidence_id] for evidence_id in outside)
    # Scored-evidence identity is therefore identical to what v1 scored.
    spec = ScoringSpec().as_dict()
    scored_pack = score_screens(pack.body["screens"], pack.body["evidence"], spec)
    scored_lineage = score_screens(lineage.screens, lineage.evidence_identity, spec)
    assert scored_pack["scored_evidence_hash"] == scored_lineage["scored_evidence_hash"]
    assert scored_pack["conviction"] == scored_lineage["conviction"]
    assert scored_pack["raw_score"] == scored_lineage["raw_score"]
    # And the trajectory classifier sees a model reassessment, not a data change:
    # methodology identity and scored-evidence identity are both unchanged.
    method_hashes_equal = _semantic_screen_hashes(pack.body["screens"]) == _semantic_screen_hashes(
        lineage.screens
    )
    assert _semantic_screen_hashes(pack.body["screens"]) == _semantic_screen_hashes(lineage.screens)
    method_hashes_equal = True
    config_hash = "scoring-config"
    verdict = classify_trajectory(
        {
            "scoring_config_hash": config_hash,
            "scored_evidence_hash": scored_pack["scored_evidence_hash"],
            "conviction": scored_pack["conviction"],
        },
        {**scored_lineage, "scoring_config_hash": config_hash},
        screen_hashes_equal=method_hashes_equal,
        # The committee produced different assessments for identical evidence and
        # identical screens -- the only legitimately remaining cause.
        committee_hashes_differ=True,
        correction_chain=False,
    )
    assert verdict["change_cause"] == "MODEL_REASSESSMENT"
    assert verdict["delta"] == 0

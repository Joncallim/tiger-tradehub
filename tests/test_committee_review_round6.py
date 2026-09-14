"""#67 review round 6 regressions.

Round 6 found no P0/P1 (all five round-5 remediations verified real) and one new
P2 plus four P3s, all in the model-facing artifact. This module pins them:

1. a shipped evidence row must never name a successor id that is not itself
   presented (pack v1 only ever named ids inside its own artifact);
2. an aggregate must not advertise the same id twice even when a series list
   repeats it;
3. ``evidence_ids_omitted`` counts declared-minus-*presented*, so a declared id
   that is visible without being advertised is not reported as omitted;
4. a feature key containing "/" cannot break aggregate resolution.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tradehub_research.committee.lineage import ScoringLineageBuilder
from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.view import CommitteeViewBuilder
from tradehub_research.db import ResearchDB
from tradehub_research.screen_store import ScreenResult, ScreenSpec
from tradehub_research.screens import canonical_json

AS_OF = "2025-06-30T00:00:00Z"


def _fixture(path: Path) -> tuple[ResearchDB, str]:
    """Series screen with a restatement chain, a repeated id and a "/" key."""
    database = ResearchDB(path)
    database.migrate()
    bars = [f"bar{index:04d}" for index in range(12)]
    # bar0001 restates bar0000; both are declared by the screen, and the middle
    # of the series is normally not sampled as a representative.
    observations = [
        {
            "evidence_id": evidence_id,
            "role": "price_bar",
            "session_date": f"2024-{index % 12 + 1:02d}-01",
            "unit": "usd_per_share",
            "value": 10.0 + index,
        }
        for index, evidence_id in enumerate(bars)
    ]
    observations.append(dict(observations[0]))  # duplicate id inside the series
    observations.append(
        {
            "evidence_id": "extra1",
            "role": "note",
            "session_date": "2024-12-01",
            "unit": "count",
            "value": 1.0,
        }
    )
    momentum_spec = ScreenSpec("momentum_confirmation", "mom", 1, 1, {}, [], "test")
    momentum = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=momentum_spec.config_hash,
        raw_features={
            # A key containing "/" must not break the shipped-copy resolution.
            "adv/20d": {"sources": observations, "unit": "usd", "value": 123.0},
        },
        evidence_ids=bars + ["extra1"],
        reason_codes=[],
        sufficient_data=True,
        passed=True,
        confidence=0.7,
        data_quality=0.5,
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
        for index, evidence_id in enumerate(bars):
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json({"record_type": "price_bar", "value": 10.0 + index}),
                    1.0,
                    "bar0000" if evidence_id == "bar0001" else None,
                    0,
                    evidence_id + "hash",
                    evidence_id,
                    "2024-06-01Z",
                    "2024-06-02Z",
                    "source_reported",
                    "2024-06-03Z",
                ),
            )
        db.execute(
            "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "extra1",
                "sec",
                "src",
                canonical_json({"record_type": "note"}),
                1.0,
                None,
                0,
                "extra1hash",
                "extra1",
                "2024-06-01Z",
                "2024-06-02Z",
                "source_reported",
                "2024-06-03Z",
            ),
        )
        db.execute(
            "INSERT INTO screen_definition VALUES (?,?,?,?,?,?)",
            (
                momentum_spec.config_hash,
                momentum_spec.family,
                momentum_spec.screen_id,
                momentum_spec.screen_version,
                momentum_spec.canonical_json(),
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
        db.execute(
            "INSERT INTO screen_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                momentum.screen_result_id,
                "run",
                "sec",
                momentum.config_hash,
                canonical_json(momentum.raw_features),
                canonical_json(momentum.evidence_ids),
                canonical_json(momentum.reason_codes),
                int(momentum.sufficient_data),
                int(momentum.passed),
                momentum.confidence,
                momentum.data_quality,
                momentum.result_hash,
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
                canonical_json([momentum.screen_result_id]),
                "{}",
                0,
                None,
                None,
                None,
                "2024-06-05Z",
            ),
        )
    return database, "cand"


def _walk_aggregates(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "lineage_set_hash" in node and "representative_evidence_ids" in node:
            found.append(node)
        for value in node.values():
            found.extend(_walk_aggregates(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_aggregates(item))
    return found


def test_no_shipped_row_names_an_invisible_successor(tmp_path):
    """Review finding P2 (round 6): supersession must stay inside the artifact."""
    database, candidate_id = _fixture(tmp_path / "supersession.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    presented = {row["evidence_id"] for row in view["evidence"]}
    assert "bar0000" in presented, "fixture must present the superseded row"
    for row in view["evidence"]:
        successor = row.get("superseded_within_pack_by")
        if successor is not None:
            assert successor in presented, f"{row['evidence_id']} names invisible {successor}"


def test_aggregate_never_advertises_a_duplicate_id(tmp_path):
    """Review finding P3 (round 6): a repeated series id must not ship twice."""
    database, candidate_id = _fixture(tmp_path / "duplicates.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    aggregates = [entry for screen in view["screens"] for entry in _walk_aggregates(screen)]
    assert aggregates
    for aggregate in aggregates:
        ids = aggregate["representative_evidence_ids"]
        assert len(ids) == len(set(ids)), "an aggregate advertised the same id twice"
        assert aggregate["observations_presented"] == len(ids)


def test_evidence_ids_omitted_counts_presented_not_advertised(tmp_path):
    """Review finding P3 (round 6): visible-but-unadvertised ids are presented."""
    database, candidate_id = _fixture(tmp_path / "omitted.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    presented = {row["evidence_id"] for row in view["evidence"]}
    declared = {f"bar{index:04d}" for index in range(12)} | {"extra1"}
    for screen in view["screens"]:
        assert set(screen["evidence_ids"]) <= presented
        # extra1 is declared and visible but is not a series representative, so it
        # is presented rather than omitted.
        expected = len(declared) - len(declared & presented)
        assert screen["evidence_ids_omitted"] == expected
        assert screen["evidence_ids_omitted"] >= 0


def test_feature_key_containing_a_slash_is_supported(tmp_path):
    """Review finding P3 (round 6): resolution must not split on "/"."""
    database, candidate_id = _fixture(tmp_path / "slash.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    momentum = next(screen for screen in view["screens"] if "adv/20d" in screen["raw_features"])
    aggregates = _walk_aggregates(momentum["raw_features"])
    assert len(aggregates) == 1
    meta = momentum["series_aggregates"]
    assert len(meta) == 1
    assert set(meta[0]["representative_evidence_ids"]) == set(
        aggregates[0]["representative_evidence_ids"]
    )
    assert momentum["evidence_ids_omitted"] >= 0
    assert all(
        evidence_id in {row["evidence_id"] for row in view["evidence"]}
        for evidence_id in meta[0]["representative_evidence_ids"]
    )


def test_view_matches_the_lineage_it_references(tmp_path):
    """Sanity: the fixture's view and lineage describe the same frozen inputs."""
    database, candidate_id = _fixture(tmp_path / "identity.db")
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    assert view.body["lineage"]["lineage_hash"] == lineage.lineage_hash
    pack = EvidencePackBuilder(database).build(candidate_id)
    # Both artifacts describe the same frozen run.
    assert pack.body["run"]["as_of"] == view.body["run"]["as_of"]
    assert view.body["candidate"]["candidate_id"] == candidate_id

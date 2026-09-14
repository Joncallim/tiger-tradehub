"""#67 review round 2 regressions.

Round 2 verified all four round-1 remediations as real and raised four new
findings. This module pins those:

1. the scoring-identity row cap must be an *independently declared* literal, and
   raising the model-facing view cap must not be able to move it (or historical
   methodology identity);
2. every series observation must be accounted for exactly once -- published as a
   citable representative row, or counted as an aggregate omission -- with no
   observation visible in ``raw_features`` but absent from ``evidence``;
3. the run -> lineage mapping must be cross-checked against the pinned view's own
   embedded lineage reference;
4. the window between creating a run and issuing its first work item must not
   admit a stale artifact.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from test_committee_identity_migration import (  # noqa: E402 - sibling test fixture
    _fixture as identity_fixture,
)
from test_committee_methodology_projection import (  # noqa: E402 - sibling test fixture
    _fixture,
)

from tradehub_research.committee import bounds as bounds_module
from tradehub_research.committee import scoring as scoring_module
from tradehub_research.committee import view as view_module
from tradehub_research.committee.lineage import ScoringLineageBuilder
from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.scoring import _semantic_screen_hashes
from tradehub_research.committee.store import CommitteeStore
from tradehub_research.committee.view import CommitteeViewBuilder
from tradehub_research.mcp_server import resolve_evidence_artifact
from tradehub_research.screen_store import DeterminismError

BOUNDS_PATH = Path(bounds_module.__file__)


def _assigned_constant_value(name: str) -> ast.expr:
    tree = ast.parse(BOUNDS_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    raise AssertionError(f"{name} is not assigned at module level in bounds.py")


def test_scoring_row_cap_is_independently_declared():
    """The scoring cap must be its own literal, never an alias of a view bound."""
    scoring_value = _assigned_constant_value("MAX_EVIDENCE_ROWS")
    assert isinstance(scoring_value, ast.Constant), (
        "MAX_EVIDENCE_ROWS must be declared as its own literal, not aliased to a view bound"
    )
    assert isinstance(scoring_value.value, int)
    assert scoring_value.value == 256
    view_value = _assigned_constant_value("MAX_VIEW_EVIDENCE_ROWS")
    assert isinstance(view_value, ast.Constant) and isinstance(view_value.value, int)


def test_raising_the_view_row_cap_cannot_move_scoring_identity(tmp_path, monkeypatch):
    """A capacity change to what models see must not touch methodology identity."""
    database, candidate_id = _fixture(tmp_path / "cap.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    pack_baseline = _semantic_screen_hashes(pack.body["screens"])
    lineage_baseline = _semantic_screen_hashes(lineage.screens)
    # The lineage carries the complete set, the legacy pack a truncated one; the
    # projection makes their methodology identity agree (round-1 fix).
    assert pack_baseline == lineage_baseline

    # Simulate a future engineer bumping the *model-facing* cap, at the source:
    # both the canonical constant and the name already bound into view's
    # namespace.
    monkeypatch.setattr(bounds_module, "MAX_VIEW_EVIDENCE_ROWS", 5000)
    monkeypatch.setattr(view_module, "MAX_VIEW_EVIDENCE_ROWS", 5000)
    assert bounds_module.MAX_EVIDENCE_ROWS == 256
    assert scoring_module.MAX_EVIDENCE_ROWS == 256
    assert _semantic_screen_hashes(pack.body["screens"]) == pack_baseline
    assert _semantic_screen_hashes(lineage.screens) == lineage_baseline

    # Changing the *scoring* cap is the deliberate act that does move identity for
    # an artifact holding the complete set -- which is why it is independently
    # declared and versioned, and why a bump requires a projection-version
    # decision rather than riding along with a view change.
    monkeypatch.setattr(scoring_module, "MAX_EVIDENCE_ROWS", 5000)
    assert _semantic_screen_hashes(lineage.screens) != lineage_baseline
    # A legacy pack stores pre-truncated ids, so it is insensitive by construction.
    assert _semantic_screen_hashes(pack.body["screens"]) == pack_baseline


def test_every_series_observation_is_accounted_exactly_once(tmp_path):
    """No observation may be visible in raw_features yet absent from evidence."""
    database, candidate_id = _fixture(tmp_path / "accounting.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    momentum = next(
        screen for screen in view["screens"] if screen["family"] == "momentum_confirmation"
    )
    aggregates = momentum["series_aggregates"]
    assert aggregates
    # Every series is aggregated regardless of size, so nothing is serialized raw.
    assert "sources" not in json.dumps(momentum["raw_features"])
    presented = {row["evidence_id"] for row in view["evidence"]}
    representatives: set[str] = set()
    for aggregate in aggregates:
        representatives.update(aggregate["representative_evidence_ids"])
        assert aggregate["observations_omitted"] == (
            aggregate["observation_count"] - aggregate["observations_presented"]
        )
    assert representatives <= presented, "advertised representatives must be citable rows"
    # Series accounting is exact: distinct observations the series reference are
    # either a presented representative or a counted aggregate omission.
    distinct_series = view["series_representation"]["distinct_observations"]
    assert distinct_series == sum(aggregate["observation_count"] for aggregate in aggregates)
    assert view["evidence_omitted"]["series_representatives_presented"] == len(
        representatives & presented
    )
    assert view["evidence_omitted"]["series_observations_omitted"] == (
        distinct_series - len(representatives & presented)
    )


def test_lineage_mapping_must_match_the_pinned_view_reference(tmp_path):
    """The mapping table must not be the caller's word for the pairing."""
    database, candidate_id = identity_fixture(tmp_path / "pairing.db")
    other_db, other_candidate = identity_fixture(tmp_path / "pairing2.db", value_base=99.0)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    other_lineage = ScoringLineageBuilder(other_db).build(other_candidate)
    assert lineage.lineage_hash != other_lineage.lineage_hash
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    with pytest.raises(DeterminismError, match="does not match the pinned view"):
        store.create_or_resume_committee_run(
            candidate_id=candidate_id,
            pack_hash=view.pack_hash,
            lineage_hash=other_lineage.lineage_hash,
            committee_policy_version=1,
            comparator_config_hash=comparator_hash,
            scoring_config_hash=scoring_hash,
            prompt_versions={"neutral": "v2", "red_team": "v2", "arbiter": "v2"},
            assessment_schema_version=1,
        )


def test_aggregate_representatives_are_always_citable_rows(tmp_path):
    """Review finding P1 (round 3): a screen's declared evidence_ids is often a
    subset of the ids its raw features reference, so sampling the raw series
    could advertise an id that never becomes an evidence row."""
    database, candidate_id = identity_fixture(tmp_path / "mismatch.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    presented = {row["evidence_id"] for row in view["evidence"]}
    frozen = set()
    with database.connect(read_only=True) as db:
        for row in db.execute("SELECT evidence_id FROM evidence_event"):
            frozen.add(row[0])
    aggregates = [entry for screen in view["screens"] for entry in screen["series_aggregates"]]
    assert aggregates
    for entry in aggregates:
        advertised = set(entry["representative_evidence_ids"])
        assert advertised <= presented, "every advertised representative must be a view row"
        assert advertised <= frozen, "advertised representatives must exist as evidence"
        assert entry["observations_presented"] == len(advertised)
        assert entry["observations_omitted"] == entry["observation_count"] - len(advertised)
    # The series here references 50 observations while only 12 exist as evidence
    # rows, so the sample must have been drawn only from the admissible 12.
    assert len(frozen) == 14  # 12 bars + val1 + val2
    for entry in aggregates:
        assert entry["observations_presented"] <= 4
    # The raw_features copy of each aggregate must agree with the summary copy.
    for screen in view["screens"]:
        for entry in screen["series_aggregates"]:
            feature = entry["path"].split("/raw_features/")[1]
            cursor: Any = screen["raw_features"]
            for part in feature.split("/"):
                cursor = cursor[part]
            assert set(cursor["representative_evidence_ids"]) == set(
                entry["representative_evidence_ids"]
            )
            assert cursor["observations_presented"] == entry["observations_presented"]
            assert cursor["observations_omitted"] == entry["observations_omitted"]


def test_pre_work_window_cannot_admit_a_stale_artifact(tmp_path):
    """Between run creation and first work issuance, only the live pin resolves."""
    database, candidate_id = identity_fixture(tmp_path / "window.db")
    stale_pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    # Create the run but deliberately do NOT initialize it (no work issued yet).
    store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=view.pack_hash,
        lineage_hash=lineage.lineage_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v2", "red_team": "v2", "arbiter": "v2"},
        assessment_schema_version=1,
    )
    with pytest.raises(ValueError, match="live or outstanding"):
        resolve_evidence_artifact(database, candidate_id)  # unpinned
    with pytest.raises(ValueError, match="not the pin of any live or outstanding"):
        resolve_evidence_artifact(database, candidate_id, stale_pack.pack_hash)
    live = resolve_evidence_artifact(database, candidate_id, view.pack_hash)
    assert live["pack_hash"] == view.pack_hash
    assert live["pinned"] is True
    assert live["body"]["lineage"]["lineage_hash"] == lineage.lineage_hash

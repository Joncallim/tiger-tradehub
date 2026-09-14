"""#67 migration safety, artifact identity pinning and legacy reproducibility.

Guards three properties the architecture depends on:

* the migration is purely additive, so a migrated database stays usable by the
  pre-#67 build (code rollback must not require a 4.6 GB database restore);
* the v1 pack representation did not move when the shared frozen-input loader
  was introduced (old committee runs pin artifact identity, not just scores);
* committee work is pinned to an exact artifact, and hashing cannot be played
  against itself.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tradehub_research.committee.frozen_inputs import PackBuildError
from tradehub_research.committee.lineage import ScoringLineageBuilder
from tradehub_research.committee.pack import EvidencePackBuilder
from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.committee.scoring import Scorer, score_screens
from tradehub_research.committee.store import CommitteeStore, ScoringSpec
from tradehub_research.committee.view import VIEW_PACK_SPEC_VERSION, CommitteeViewBuilder
from tradehub_research.db import ResearchDB
from tradehub_research.mcp_server import resolve_evidence_artifact
from tradehub_research.schema import MIGRATIONS
from tradehub_research.screens import ScreenResult, ScreenSpec, canonical_json

# ruff: noqa: E501 -- fixture SQL mirrors complete immutable table layouts.
AS_OF = "2025-06-30T00:00:00Z"

#: The pre-refactor (#66) builder's output for the fixture below, captured by
#: running the same fixture against main@6a4154d.  If the shared loader ever
#: changes this value, artifact identity for old committee runs has moved.
LEGACY_PACK_HASH = "ea586446d850c036e52e1d41b34ba666be817c9d536e1c7a4d129c092c305eb2"
LEGACY_BODY_CHARS = 17572
LEGACY_BODY_SHA256 = "1f1e7bb61580a3150faaf51a91f5beab2ffac7ba2ef11ff649dcf85b92b768a9"

#: The historical physical shape of committee_run.  Rollback compatibility
#: depends on this list never changing.
FROZEN_COMMITTEE_RUN_COLUMNS = [
    "committee_run_id",
    "candidate_id",
    "pipeline_run_id",
    "pack_hash",
    "role_set_json",
    "committee_policy_version",
    "comparator_config_hash",
    "scoring_config_hash",
    "prompt_versions_json",
    "assessment_schema_version",
    "created_at",
]


def _fixture(
    path: Path, *, value_base: float = 10.0, bars: int = 12, withdrawn_val1: bool = False
) -> tuple[ResearchDB, str]:
    database = ResearchDB(path)
    database.migrate()
    momentum_spec = ScreenSpec("momentum_confirmation", "mom", 1, 1, {}, [], "test")
    valuation_spec = ScreenSpec("valuation", "value", 1, 1, {}, [], "test")
    observations = [
        {
            "evidence_id": f"bar{index:04d}",
            "role": "price_bar",
            "session_date": f"2024-{index % 12 + 1:02d}-{index % 28 + 1:02d}",
            "unit": "usd_per_share",
            "value": value_base + index,
        }
        for index in range(50)
    ]
    momentum = ScreenResult.create(
        run_id="run",
        security_id="sec",
        config_hash=momentum_spec.config_hash,
        raw_features={
            "adv_20d": {"sources": observations, "unit": "usd", "value": 63414310.24},
            "eligible_bar_count": {"sources": observations, "unit": "count", "value": 50.0},
        },
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
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    canonical_json({"record_type": "price_bar", "value": value_base + index}),
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
        for evidence_id in ("val1", "val2"):
            is_withdrawn = withdrawn_val1 and evidence_id == "val1"
            db.execute(
                "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    "sec",
                    "src",
                    "{}"
                    if is_withdrawn
                    else canonical_json(
                        {"record_type": "xbrl_fact", "accession": "acc", "value": 1}
                    ),
                    1.0,
                    None,
                    1 if is_withdrawn else 0,
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


def test_migration_12_is_purely_additive():
    """A migrated database must stay usable by pre-#67 code: additive only."""
    import re

    for version, _description, sql in MIGRATIONS:
        if version < 12:
            continue
        # Trigger bodies contain ';' before END, so strip them before splitting.
        without_triggers = re.sub(r"CREATE TRIGGER.*?END;", "", sql, flags=re.DOTALL)
        statements = [part.strip().upper() for part in without_triggers.split(";") if part.strip()]
        for statement in statements:
            assert statement.startswith(("CREATE TABLE", "CREATE INDEX")), (
                f"migration {version} is not purely additive: {statement[:60]}"
            )
        for forbidden in ("ALTER TABLE", "DROP TABLE", "DROP COLUMN", "DELETE FROM", "UPDATE "):
            assert forbidden not in without_triggers.upper(), (
                f"migration {version} contains {forbidden}"
            )
        assert "CREATE TRIGGER" in sql.upper()


def test_committee_run_shape_is_frozen_and_mapping_table_is_append_only(tmp_path):
    database, candidate_id = _fixture(tmp_path / "shape.db")
    with database.connect(read_only=True) as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(committee_run)")]
        mapping = [row[1] for row in db.execute("PRAGMA table_info(committee_run_lineage)")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert columns == FROZEN_COMMITTEE_RUN_COLUMNS
    assert mapping == ["committee_run_id", "lineage_hash", "recorded_at"]
    assert {"scoring_lineage", "committee_run_lineage"} <= tables

    # The pre-#67 positional insert shape (11 values, no column list) still works
    # on a migrated database: this is what code rollback depends on.
    pack = EvidencePackBuilder(database).build(candidate_id)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    with database.connect() as db:
        db.execute(
            "INSERT INTO committee_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-shaped-run",
                candidate_id,
                "run",
                pack.pack_hash,
                "[]",
                1,
                comparator_hash,
                scoring_hash,
                "{}",
                1,
                "2024-06-05Z",
            ),
        )
    # The mapping table is append-only (a populated row cannot be deleted or
    # repointed), while the historical committee_run shape carries no new column.
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    run_id = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=view.pack_hash,
        lineage_hash=lineage.lineage_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v2", "red_team": "v2", "arbiter": "v2"},
        assessment_schema_version=1,
    )
    with database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT lineage_hash FROM committee_run_lineage WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
            == lineage.lineage_hash
        )
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM committee_run_lineage WHERE committee_run_id=?", (run_id,))


def test_v1_pack_hash_is_reproduced_after_the_shared_loader_refactor(tmp_path):
    """Old committee runs pin artifact identity: the v1 body must not move."""
    import hashlib

    database, candidate_id = _fixture(tmp_path / "repro.db")
    with database.connect() as db:
        rebuilt = EvidencePackBuilder(database)._build(db, candidate_id)
    body_json = canonical_json(rebuilt.body)
    assert rebuilt.pack_hash == LEGACY_PACK_HASH
    assert len(body_json) == LEGACY_BODY_CHARS
    assert hashlib.sha256(body_json.encode()).hexdigest() == LEGACY_BODY_SHA256


def test_artifact_hashes_are_deterministic_and_not_substitutable(tmp_path):
    database, candidate_id = _fixture(tmp_path / "identity.db")
    lineage_first = ScoringLineageBuilder(database).build(candidate_id)
    lineage_again = ScoringLineageBuilder(database).build(candidate_id)
    view_first = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage_first)
    view_again = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage_first)
    assert lineage_first.lineage_hash == lineage_again.lineage_hash
    assert view_first.pack_hash == view_again.pack_hash
    assert view_first.pack_hash != lineage_first.lineage_hash
    assert view_first.body["lineage"]["lineage_hash"] == lineage_first.lineage_hash

    # One row each: rebuilds reuse, never duplicate.
    with database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM scoring_lineage WHERE candidate_id=?", (candidate_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM evidence_pack WHERE candidate_id=? AND pack_spec_version=?",
                (candidate_id, VIEW_PACK_SPEC_VERSION),
            ).fetchone()[0]
            == 1
        )

    # A changed frozen input changes lineage identity and never overwrites.
    other_db, other_candidate = _fixture(tmp_path / "identity2.db", value_base=99.0)
    changed = ScoringLineageBuilder(other_db).build(other_candidate)
    assert changed.lineage_hash != lineage_first.lineage_hash

    # The lineage cannot be served as the model view and the view cannot be scored.
    assert lineage_first.body["lineage_spec_version"] == 1
    assert view_first.body["view_spec_version"] == 1
    with database.connect(read_only=True) as db:
        stored_view = db.execute(
            "SELECT body_json FROM evidence_pack WHERE candidate_id=? AND pack_spec_version=?",
            (candidate_id, VIEW_PACK_SPEC_VERSION),
        ).fetchone()[0]
    assert "evidence_identity" not in json.loads(stored_view)


def test_scoring_consumes_lineage_and_refuses_a_view(tmp_path):
    database, candidate_id = _fixture(tmp_path / "scoring.db")
    pack = EvidencePackBuilder(database).build(candidate_id)
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    run_id = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=view.pack_hash,
        lineage_hash=lineage.lineage_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v2", "red_team": "v2", "arbiter": "v2"},
        assessment_schema_version=1,
    )
    with database.connect(read_only=True) as db:
        row = db.execute(
            "SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)
        ).fetchone()
        screens, evidence, kind = Scorer._scoring_artifact(db, row)
    assert kind.startswith("lineage:")
    assert screens != view.body["screens"]
    assert evidence != view.body["evidence"]
    assert len(evidence) == 14  # 12 bars + val1 + val2, the complete frozen set
    # Scoring the bounded view directly must not be possible through the resolver.
    with database.connect() as db:
        db.execute(
            "INSERT INTO committee_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "view-pinned",
                candidate_id,
                "run",
                view.pack_hash,
                "[]",
                1,
                comparator_hash,
                scoring_hash,
                "{}",
                1,
                "2024-06-05Z",
            ),
        )
        orphan = db.execute(
            "SELECT * FROM committee_run WHERE committee_run_id='view-pinned'"
        ).fetchone()
        with pytest.raises(ValueError, match="must never be a scoring input"):
            Scorer._scoring_artifact(db, orphan)
    # Legacy compatibility path is unchanged where a v1 pack is pinned.
    assert score_screens(
        pack.body["screens"], pack.body["evidence"], ScoringSpec().as_dict()
    ) == score_screens(screens, evidence, ScoringSpec().as_dict())


def test_work_is_pinned_to_one_artifact_and_races_fail_closed(tmp_path):
    database, candidate_id = _fixture(tmp_path / "pin.db")
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()

    legacy_pack = EvidencePackBuilder(database).build(candidate_id)
    legacy_run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=legacy_pack.pack_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
        assessment_schema_version=1,
    )
    router = CommitteeRouter(database)
    router.initialize(legacy_run)
    envelope = router.get_work(legacy_run)
    assert envelope is not None
    pinned_v1 = envelope["pack_hash"]
    assert pinned_v1 == legacy_pack.pack_hash

    # A newer artifact appears AFTER the work was issued.
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    new_run = store.create_or_resume_committee_run(
        candidate_id=candidate_id,
        pack_hash=view.pack_hash,
        lineage_hash=lineage.lineage_hash,
        committee_policy_version=1,
        comparator_config_hash=comparator_hash,
        scoring_config_hash=scoring_hash,
        prompt_versions={"neutral": "v2", "red_team": "v2", "arbiter": "v2"},
        assessment_schema_version=1,
    )
    assert new_run != legacy_run

    # The race cannot change what the issued work sees.
    old_work = resolve_evidence_artifact(database, candidate_id, pinned_v1)
    assert old_work["body"]["pack_spec_version"] == 1
    assert old_work["pack_hash"] == pinned_v1
    assert old_work["representation"] == "LEGACY_SCORING_PACK"
    # A different existing artifact for the same candidate is refused while the
    # outstanding work is pinned elsewhere: fail closed before any model spend.
    with pytest.raises(ValueError, match="not the pin of any outstanding committee work"):
        resolve_evidence_artifact(database, candidate_id, view.pack_hash)
    # Once the newer run's own work is issued, its own pin resolves.
    router.initialize(new_run)
    new_envelope = router.get_work(new_run)
    assert new_envelope is not None
    assert new_envelope["pack_hash"] == view.pack_hash
    new_work = resolve_evidence_artifact(database, candidate_id, view.pack_hash)
    assert new_work["body"]["view_spec_version"] == 1
    assert new_work["pinned"] is True
    assert new_work["lineage_hash"] == lineage.lineage_hash

    # Wrong or unknown pins fail closed (here with outstanding work, the refusal
    # is the stronger "not the pin of any outstanding committee work" check), and
    # unpinned lookups are refused while work is outstanding.
    with pytest.raises(ValueError, match="not the pin of any outstanding committee work"):
        resolve_evidence_artifact(database, candidate_id, "0" * 64)
    with pytest.raises(ValueError, match="unpinned evidence lookup refused"):
        resolve_evidence_artifact(database, candidate_id)
    # The work row itself carries the same pin the run does.
    with database.connect(read_only=True) as db:
        assert (
            db.execute(
                "SELECT pack_hash FROM committee_work WHERE committee_run_id=?", (legacy_run,)
            ).fetchone()[0]
            == pinned_v1
        )
        assert (
            db.execute(
                "SELECT pack_hash FROM committee_run WHERE committee_run_id=?", (legacy_run,)
            ).fetchone()[0]
            == pinned_v1
        )
    # The fabricated unpinned convenience path is refused while work is open, so
    # the only way to obtain an artifact for this candidate is by exact pin.
    assert new_work["pinned"] is True


def test_both_artifacts_bind_to_identical_frozen_inputs(tmp_path):
    database, candidate_id = _fixture(tmp_path / "pit.db")
    lineage = ScoringLineageBuilder(database).build(candidate_id)
    view = CommitteeViewBuilder(database).build(candidate_id, lineage=lineage)
    assert lineage.body["run"] == view.body["run"]
    assert lineage.body["candidate"] == view.body["candidate"]
    assert lineage.body["identity"] == view.body["identity"]
    assert lineage.body["run"]["as_of"] == AS_OF
    lineage_ids = {row["evidence_id"] for row in lineage.evidence_identity}
    view_ids = {row["evidence_id"] for row in view.body["evidence"]}
    assert view_ids <= lineage_ids
    for row in lineage.evidence_identity:
        assert row["public_available_time"] <= AS_OF
    assert view.body["lineage"]["evidence_identity_count"] == len(lineage_ids)
    assert lineage.body["counts"]["frozen_evidence"] == len(lineage_ids)


def test_pack_build_error_codes_survive_the_refactor(tmp_path):
    """The shared loader must not soften any pre-existing gate."""
    database, candidate_id = _fixture(tmp_path / "gates.db", withdrawn_val1=True)
    for builder in (
        lambda: EvidencePackBuilder(database).build(candidate_id),
        lambda: ScoringLineageBuilder(database).build(candidate_id),
        lambda: CommitteeViewBuilder(database).build(candidate_id),
    ):
        with pytest.raises(PackBuildError) as failure:
            builder()
        assert failure.value.code == "EVIDENCE_WITHDRAWN"

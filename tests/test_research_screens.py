from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.schema import MIGRATIONS, PHASE_0_SCHEMA_VERSION
from tradehub_research.screen_store import DeterminismError, RunStateError, ScreenStore
from tradehub_research.screens import (
    SCREEN_REGISTRY,
    ScreenResult,
    ScreenResultPayload,
    ScreenSpec,
    canonical_json,
    get_screen,
    register_screen,
    registered_screens,
    screen_result_id,
)


@pytest.fixture
def database(tmp_path):
    value = ResearchDB(tmp_path / "research.db")
    value.migrate()
    with value.connect() as db:
        db.execute(
            "INSERT INTO security VALUES "
            "('s1','AAA','NYSE','A',NULL,NULL,'SUPPORTED','2024-01-01T00:00:00Z',NULL),"
            "('s2','BBB','NYSE','B',NULL,NULL,'SUPPORTED','2024-01-01T00:00:00Z',NULL)"
        )
    return value


@pytest.fixture
def spec():
    return ScreenSpec("valuation", "value", 1, 1, {"floor": 0.05}, ["income"], "value-v1")


def start(store, spec, *, count=2):
    store.save_screen_definition(spec)
    return store.begin_run(
        as_of="2025-01-01T00:00:00+00:00",
        universe_hash="universe",
        screen_manifest=[{"config_hash": spec.config_hash, "expected_count": count}],
        funnel_config={"budget": 50},
        input_view_hash="view",
        expected_security_count=count,
    )


def result(run_id, spec, security_id="s1", *, passed=True, computed_at=None):
    return ScreenResult.create(
        run_id=run_id,
        security_id=security_id,
        config_hash=spec.config_hash,
        raw_features={"yield": {"value": 0.06, "unit": "ratio"}},
        evidence_ids=["e2", "e1", "e1"],
        reason_codes=["pass", "pass"],
        sufficient_data=True,
        passed=passed,
        confidence=0.8,
        data_quality=0.9,
        computed_at=computed_at,
    )


def test_schema_v6_applies_fresh(database):
    assert database.schema_version() == PHASE_0_SCHEMA_VERSION == 6
    with database.connect(read_only=True) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"screen_definition", "pipeline_run", "screen_result", "candidate"} <= tables


def test_schema_v6_migrates_a_v5_database(tmp_path):
    database = ResearchDB(tmp_path / "v5.db")
    with database.connect() as db:
        db.execute(
            "CREATE TABLE schema_version(version_id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
            "description TEXT NOT NULL)"
        )
        for version, description, sql in MIGRATIONS[:5]:
            db.executescript(sql)
            db.execute("INSERT INTO schema_version VALUES (?,?,?)", (version, "now", description))
    assert database.schema_version() == 5
    assert database.migrate() == 6


def test_screen_spec_canonical_hash_is_order_independent():
    left = ScreenSpec("quality", "q", 1, 2, {"b": 2, "a": {"y": 1, "x": 0}}, ["x"], "q1")
    right = ScreenSpec("quality", "q", 1, 2, {"a": {"x": 0, "y": 1}, "b": 2}, ["x"], "q1")
    assert left.canonical_json() == right.canonical_json()
    assert left.config_hash == right.config_hash
    assert " " not in left.canonical_json()


def test_screen_definition_save_and_identical_reuse(database, spec):
    store = ScreenStore(database)
    assert store.save_screen_definition(spec) == spec.config_hash
    assert store.save_screen_definition(spec) == spec.config_hash


def test_screen_definition_version_collision(database, spec):
    store = ScreenStore(database)
    store.save_screen_definition(spec)
    with pytest.raises(DeterminismError):
        store.save_screen_definition(replace(spec, parameters={"floor": 0.06}))


def test_registry_register_lookup_and_stable_order(spec):
    old = SCREEN_REGISTRY.copy()
    SCREEN_REGISTRY.clear()

    def hunter(_context, _security):
        return ScreenResultPayload({}, [], [], False, False, 0, 0)

    try:
        register_screen(spec, hunter)
        register_screen(spec, hunter)
        assert get_screen("valuation", "value", 1) == (spec, hunter)
        assert registered_screens() == ((spec, hunter),)
        with pytest.raises(ValueError):
            register_screen(replace(spec, implementation_id="changed"), hunter)
    finally:
        SCREEN_REGISTRY.clear()
        SCREEN_REGISTRY.update(old)


def test_begin_run_is_deterministic_and_reuses_running(database, spec):
    store = ScreenStore(database)
    first = start(store, spec)
    second = start(store, spec)
    assert first == second
    assert (
        first
        == hashlib.sha256(
            (
                "pipeline-v1\0"
                "2025-01-01T00:00:00Zuniverse"
                + hashlib.sha256(
                    canonical_json(
                        [{"config_hash": spec.config_hash, "expected_count": 2}]
                    ).encode()
                ).hexdigest()
                + hashlib.sha256(canonical_json({"budget": 50}).encode()).hexdigest()
                + "view"
            ).encode()
        ).hexdigest()
    )


def test_begin_run_reuses_complete_but_failed_is_terminal(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec, count=0)
    store.complete_run(run_id)
    assert start(store, spec, count=0) == run_id

    other = replace(spec, screen_version=2)
    failed_id = start(store, other, count=0)
    store.fail_run(failed_id, "bad input")
    with pytest.raises(RunStateError):
        start(store, other, count=0)


def test_result_id_hash_and_normalization_are_deterministic(spec):
    first = result("run", spec, computed_at="2025-01-01T01:00:00Z")
    second = result("run", spec, computed_at="2026-01-01T01:00:00Z")
    assert first.screen_result_id == screen_result_id("run", "s1", spec.config_hash)
    assert first.result_hash == second.result_hash
    assert first.evidence_ids == ["e1", "e2"]
    assert first.reason_codes == ["pass"]


def test_result_hash_covers_every_logical_field(spec):
    original = result("run", spec)
    for field, value in (("confidence", 0.7), ("data_quality", 0.7), ("passed", False)):
        changed = replace(original, **{field: value})
        with pytest.raises(ValueError, match="result_hash"):
            changed.verify()


def test_result_contract_rejects_invalid_tristate_and_bounds(spec):
    kwargs = dict(
        run_id="run",
        security_id="s1",
        config_hash=spec.config_hash,
        raw_features={},
        evidence_ids=[],
        reason_codes=[],
        data_quality=1.0,
    )
    with pytest.raises(ValueError):
        ScreenResult.create(**kwargs, sufficient_data=False, passed=True, confidence=0)
    with pytest.raises(ValueError):
        ScreenResult.create(**kwargs, sufficient_data=False, passed=False, confidence=0.1)
    with pytest.raises(ValueError):
        ScreenResult.create(**kwargs, sufficient_data=True, passed=False, confidence=1.1)


def test_population_insert_and_identical_retry_ignores_computed_at(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    store.persist_screen_population(run_id, spec.config_hash, [result(run_id, spec)])
    store.persist_screen_population(
        run_id,
        spec.config_hash,
        [result(run_id, spec, computed_at="2030-01-01T00:00:00Z")],
    )
    assert store.count_screen_results(run_id, spec.config_hash) == 1


def test_differing_retry_rolls_back_population_and_fails_run(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    store.persist_screen_population(run_id, spec.config_hash, [result(run_id, spec)])
    different = ScreenResult.create(
        run_id=run_id,
        security_id="s1",
        config_hash=spec.config_hash,
        raw_features={"yield": None},
        evidence_ids=[],
        reason_codes=["missing"],
        sufficient_data=False,
        passed=False,
        confidence=0,
        data_quality=0,
    )
    with pytest.raises(DeterminismError):
        store.persist_screen_population(run_id, spec.config_hash, [different])
    with database.connect(read_only=True) as db:
        status, failure = db.execute(
            "SELECT status,failure_json FROM pipeline_run WHERE run_id=?", (run_id,)
        ).fetchone()
    assert status == "FAILED"
    assert "deterministic retry" in json.loads(failure)["reason"]


def test_population_verification_checks_exact_security_set(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    store.persist_screen_population(
        run_id, spec.config_hash, [result(run_id, spec, "s1"), result(run_id, spec, "s2")]
    )
    assert store.verify_screen_population(run_id, spec.config_hash, ["s2", "s1"])
    assert not store.verify_screen_population(run_id, spec.config_hash, ["s1"])
    assert store.verify_expected_counts(run_id)


def test_complete_requires_all_expected_rows(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    store.persist_screen_population(run_id, spec.config_hash, [result(run_id, spec)])
    with pytest.raises(RunStateError, match="incomplete"):
        store.complete_run(run_id)


def test_complete_allows_status_transition_and_blocks_new_results(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    rows = [result(run_id, spec, "s1"), result(run_id, spec, "s2")]
    store.persist_screen_population(run_id, spec.config_hash, rows)
    store.complete_run(run_id)
    store.complete_run(run_id)
    store.persist_screen_population(run_id, spec.config_hash, rows)
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "INSERT INTO screen_result SELECT 'new-id',run_id,security_id,config_hash,"
            "raw_features_json,evidence_ids_json,reason_codes_json,sufficient_data,passed,"
            "confidence,data_quality,result_hash,computed_at FROM screen_result LIMIT 1"
        )


def test_pipeline_manifest_and_hash_inputs_are_immutable(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE pipeline_run SET screen_manifest_hash='changed' WHERE run_id=?", (run_id,)
        )


def test_result_and_candidate_rows_are_structurally_append_only(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec)
    store.persist_screen_population(run_id, spec.config_hash, [result(run_id, spec)])
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE screen_result SET confidence=0.5")


def test_candidate_write_after_complete_is_rejected(database, spec):
    store = ScreenStore(database)
    run_id = start(store, spec, count=0)
    store.complete_run(run_id)
    with database.connect() as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "INSERT INTO candidate VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("c", run_id, "s1", 1, "[]", "[]", "{}", 0, None, None, None, "now"),
        )

import sqlite3
from datetime import date

import pytest

from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import (
    append_outcome,
    record_prediction,
    record_prediction_from_screen,
)


def test_prediction_is_immutable_before_outcome(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    prediction_id = record_prediction(
        experiment_db,
        security_id="sec-1",
        as_of="2024-01-01T00:00:00Z",
        variant_name="production",
        score_value=0.7,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"momentum": 0.05},
        config_hash="cfg-1",
        evidence_ids=["ev-1"],
        horizon_sessions=63,
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with experiment_db.connect() as conn:
            conn.execute(
                "UPDATE forward_prediction SET score_value=0.9 WHERE prediction_id=?",
                (prediction_id,),
            )


def test_outcome_append_does_not_mutate_prediction(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    prediction_id = record_prediction(
        experiment_db,
        security_id="sec-1",
        as_of="2024-01-01T00:00:00Z",
        variant_name="production",
        score_value=0.7,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"momentum": 0.05},
        config_hash="cfg-1",
        evidence_ids=["ev-1"],
        horizon_sessions=63,
    )

    append_outcome(
        experiment_db,
        prediction_id=prediction_id,
        outcome_status="OBSERVED",
        total_return=0.03,
    )

    with experiment_db.connect(read_only=True) as conn:
        prediction = conn.execute(
            "SELECT score_value FROM forward_prediction WHERE prediction_id=?",
            (prediction_id,),
        ).fetchone()
        outcome = conn.execute(
            "SELECT total_return FROM forward_outcome WHERE prediction_id=?",
            (prediction_id,),
        ).fetchone()
    assert prediction["score_value"] == 0.7  # unchanged by outcome append
    assert outcome["total_return"] == 0.03  # outcome lives in its own row


def test_prediction_dedupes_identical_and_rejects_tamper(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    first = record_prediction(
        experiment_db,
        security_id="sec-1",
        as_of="2024-01-01T00:00:00Z",
        variant_name="production",
        score_value=0.7,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"momentum": 0.05},
        config_hash="cfg-1",
        evidence_ids=["ev-1"],
        horizon_sessions=63,
    )
    # Identical content dedupes to the SAME prediction (no duplicate row).
    second = record_prediction(
        experiment_db,
        security_id="sec-1",
        as_of="2024-01-01T00:00:00Z",
        variant_name="production",
        score_value=0.7,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"momentum": 0.05},
        config_hash="cfg-1",
        evidence_ids=["ev-1"],
        horizon_sessions=63,
    )
    assert first == second

    # A colliding re-record with DIFFERENT content is a data-integrity
    # error -- the first prediction is immutable and authoritative, and
    # dedupe must never silently alias different features (tamper-adjacent).
    with pytest.raises(ValueError, match="different raw_features_hash"):
        record_prediction(
            experiment_db,
            security_id="sec-1",
            as_of="2024-01-01T00:00:00Z",
            variant_name="production",
            score_value=0.99,  # would-be tamper
            state=None,
            screen_passed=True,
            sufficient_data=True,
            raw_features={"momentum": 0.99},
            config_hash="cfg-1",
            evidence_ids=["ev-1"],
            horizon_sessions=63,
        )

    with experiment_db.connect(read_only=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM forward_prediction").fetchone()[0]
        score = conn.execute(
            "SELECT score_value FROM forward_prediction WHERE prediction_id=?", (first,)
        ).fetchone()[0]
    assert count == 1
    assert score == 0.7  # original value retained


def test_record_from_screen_keeps_fail_and_insufficient(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    for screen in (
        {
            "security_id": "a",
            "computed_at": "2024-01-01T00:00:00Z",
            "passed": 1,
            "sufficient_data": 1,
            "confidence": 0.8,
            "config_hash": "cfg-1",
            "raw_features_json": '{"m": 1}',
            "evidence_ids_json": '["e1"]',
        },
        {
            "security_id": "b",
            "computed_at": "2024-01-01T00:00:00Z",
            "passed": 0,
            "sufficient_data": 1,
            "confidence": 0.2,
            "config_hash": "cfg-1",
            "raw_features_json": '{"m": -1}',
            "evidence_ids_json": '["e2"]',
        },
        {
            "security_id": "c",
            "computed_at": "2024-01-01T00:00:00Z",
            "passed": 0,
            "sufficient_data": 0,
            "confidence": 0.0,
            "config_hash": "cfg-1",
            "raw_features_json": "{}",
            "evidence_ids_json": "[]",
        },
    ):
        record_prediction_from_screen(experiment_db, screen=screen, horizon_sessions=63)

    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT security_id, screen_passed, sufficient_data FROM forward_prediction"
        ).fetchall()
    by_security = {row["security_id"]: row for row in rows}
    assert set(by_security) == {"a", "b", "c"}
    assert by_security["b"]["screen_passed"] == 0  # FAIL retained
    assert by_security["c"]["sufficient_data"] == 0  # insufficient retained


def test_record_all_screens_family_scoped_and_idempotent(tmp_path):
    from tradehub_research.validation.forward_collector import record_all_screen_predictions

    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    screens = [
        {
            "security_id": "a",
            "computed_at": "2024-01-01T00:00:00Z",
            "family": "valuation",
            "passed": 1,
            "sufficient_data": 1,
            "confidence": 0.8,
            "config_hash": "cfg-v",
            "raw_features_json": "{}",
            "evidence_ids_json": "[]",
        },
        {
            "security_id": "a",
            "computed_at": "2024-01-01T00:00:00Z",
            "family": "momentum_confirmation",
            "passed": 1,
            "sufficient_data": 1,
            "confidence": 0.9,
            "config_hash": "cfg-m",
            "raw_features_json": "{}",
            "evidence_ids_json": "[]",
        },
    ]
    counts = record_all_screen_predictions(experiment_db, screens=screens, horizons=(21,))
    assert counts["a"] == 2
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT variant_name, provenance FROM forward_prediction ORDER BY variant_name"
        ).fetchall()
    assert [r["variant_name"] for r in rows] == [
        "production/momentum_confirmation",
        "production/valuation",
    ]
    # The bulk replay path classifies rows as replay_bootstrap: they can
    # never enter forward-evidence calculations (production-only queries).
    assert {r["provenance"] for r in rows} == {"replay_bootstrap"}
    # Idempotent: same screens again -> no new rows.
    record_all_screen_predictions(experiment_db, screens=screens, horizons=(21,))
    with experiment_db.connect(read_only=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM forward_prediction").fetchone()[0]
    assert n == 2


def _screen(security_id: str, computed_at: str, family: str = "valuation") -> dict:
    return {
        "security_id": security_id,
        "computed_at": computed_at,
        "family": family,
        "passed": 1,
        "sufficient_data": 1,
        "confidence": 0.8,
        "config_hash": "cfg-v",
        "raw_features_json": "{}",
        "evidence_ids_json": "[]",
    }


def test_future_dated_screen_cannot_create_forward_prediction(tmp_path):
    """A production screen with as_of AFTER the collection date is rejected
    -- never silently clamped to today."""
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    with pytest.raises(ValueError, match="future-dated forward prediction rejected"):
        record_prediction(
            experiment_db,
            security_id="a",
            as_of="2027-06-01T20:15:00Z",
            variant_name="production",
            score_value=0.8,
            state=None,
            screen_passed=True,
            sufficient_data=True,
            raw_features={},
            config_hash="cfg-v",
            evidence_ids=[],
            horizon_sessions=21,
            collection_date=date(2026, 8, 29),
        )
    with experiment_db.connect(read_only=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM forward_prediction").fetchone()[0]
    assert n == 0  # rejected row never lands


def test_same_day_production_screen_can_record(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    pid = record_prediction(
        experiment_db,
        security_id="a",
        as_of="2026-08-29T20:15:00Z",
        variant_name="production",
        score_value=0.8,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={},
        config_hash="cfg-v",
        evidence_ids=[],
        horizon_sessions=21,
        collection_date=date(2026, 8, 29),
    )
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT as_of, provenance FROM forward_prediction WHERE prediction_id=?",
            (pid,),
        ).fetchone()
    assert row["as_of"] == "2026-08-29"
    assert row["provenance"] == "production"


def test_past_screen_with_delayed_collection_is_honest(tmp_path):
    """A past legitimate production screen may be captured later (delayed
    collection) -- the guard allows as_of <= collection and created_at
    records the honest collection time."""
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    pid = record_prediction(
        experiment_db,
        security_id="a",
        as_of="2026-08-28T20:15:00Z",  # yesterday's production screen
        variant_name="production",
        score_value=0.8,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={},
        config_hash="cfg-v",
        evidence_ids=[],
        horizon_sessions=21,
        collection_date=date(2026, 8, 29),  # collected today
    )
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT as_of, created_at, provenance FROM forward_prediction WHERE prediction_id=?",
            (pid,),
        ).fetchone()
    assert row["as_of"] == "2026-08-28"
    assert row["provenance"] == "production"
    assert row["created_at"].startswith("2026-08-29")  # collection time recorded honestly


def test_dedupe_and_immutability_survive_the_guard(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    pid1 = record_prediction(
        experiment_db,
        security_id="a",
        as_of="2026-08-29T20:15:00Z",
        variant_name="production",
        score_value=0.8,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"x": 1},
        config_hash="cfg-v",
        evidence_ids=[],
        horizon_sessions=21,
        collection_date=date(2026, 8, 29),
    )
    # Identical re-record dedupes to the SAME id; different features collide.
    pid2 = record_prediction(
        experiment_db,
        security_id="a",
        as_of="2026-08-29T20:15:00Z",
        variant_name="production",
        score_value=0.8,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"x": 1},
        config_hash="cfg-v",
        evidence_ids=[],
        horizon_sessions=21,
        collection_date=date(2026, 8, 29),
    )
    assert pid1 == pid2
    with pytest.raises(ValueError, match="collision"):
        record_prediction(
            experiment_db,
            security_id="a",
            as_of="2026-08-29T20:15:00Z",
            variant_name="production",
            score_value=0.9,
            state=None,
            screen_passed=True,
            sufficient_data=True,
            raw_features={"x": 2},  # different content
            config_hash="cfg-v",
            evidence_ids=[],
            horizon_sessions=21,
            collection_date=date(2026, 8, 29),
        )
    # Immutability: UPDATE is structurally blocked.
    with pytest.raises(sqlite3.Error):
        with experiment_db.connect() as conn:
            conn.execute(
                "UPDATE forward_prediction SET score_value=0.0 WHERE prediction_id=?", (pid1,)
            )


def test_replay_grid_cannot_bulk_populate_live_forward_ledger(tmp_path):
    """The replay/bulk path (a) rejects future-dated grids outright and
    (b) classifies any past rows as replay_bootstrap -- production-only
    forward-evidence queries never see them."""
    from tradehub_research.validation.forward_collector import record_all_screen_predictions

    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    future_screens = [_screen("a", "2027-06-01T20:15:00Z")]
    with pytest.raises(ValueError, match="future-dated forward prediction rejected"):
        record_all_screen_predictions(experiment_db, screens=future_screens, horizons=(21,))
    with experiment_db.connect(read_only=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM forward_prediction").fetchone()[0]
    assert n == 0
    # A PAST replay grid lands as replay_bootstrap: the production ledger is
    # still empty.
    past_screens = [_screen("a", "2026-08-28T20:15:00Z"), _screen("b", "2026-08-28T20:15:00Z")]
    record_all_screen_predictions(experiment_db, screens=past_screens, horizons=(21,))
    with experiment_db.connect(read_only=True) as conn:
        prod = conn.execute(
            "SELECT COUNT(*) FROM forward_prediction WHERE provenance='production'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM forward_prediction").fetchone()[0]
    assert prod == 0
    assert total == 2

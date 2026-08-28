import sqlite3

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
            "SELECT variant_name FROM forward_prediction ORDER BY variant_name"
        ).fetchall()
    assert [r["variant_name"] for r in rows] == [
        "production/momentum_confirmation",
        "production/valuation",
    ]
    # Idempotent: same screens again -> no new rows.
    record_all_screen_predictions(experiment_db, screens=screens, horizons=(21,))
    with experiment_db.connect(read_only=True) as conn:
        n = conn.execute("SELECT COUNT(*) FROM forward_prediction").fetchone()[0]
    assert n == 2

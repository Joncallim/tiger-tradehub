import pytest

from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.holdout import run_sealed_holdout
from tradehub_research.validation.regime import draft_evaluation_regime
from tradehub_research.validation.walk_forward import (
    walk_forward_folds,
)


def _seed_regime(experiment_db, snapshot_id="snap-1"):
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot VALUES "
            f"('{snapshot_id}','abc',11,NULL,'{{}}','h1','/tmp/x','h2','{{}}','READY','2025-01-01T00:00:00Z')"
        )


def _synthetic_screens():
    screens = []
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01", "2024-06-01"):
        for sid, confidence in (("a", 0.9), ("b", 0.7), ("c", 0.5), ("d", 0.3)):
            screens.append(
                {
                    "screen_result_id": f"{day}-{sid}",
                    "run_id": f"run-{day}",
                    "security_id": sid,
                    "config_hash": "cfg-x",
                    "family": "valuation",
                    "passed": confidence >= 0.6,
                    "sufficient_data": True,
                    "confidence": confidence,
                    "data_quality": 0.9,
                    "computed_at": f"{day}T00:00:00Z",
                }
            )
    return screens


def _synthetic_outcomes():
    labels = []
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01", "2024-06-01"):
        for sid, magnitude in (("a", 0.10), ("b", 0.05), ("c", -0.02), ("d", -0.08)):
            for horizon in (21, 63):
                labels.append(
                    {
                        "label_id": f"{day}-{sid}-{horizon}",
                        "dataset_snapshot_id": "snap-1",
                        "security_id": sid,
                        "observation_date": day,
                        "horizon_sessions": horizon,
                        "outcome_status": "OBSERVED",
                        "benchmark_relative_return": magnitude * (horizon / 21),
                    }
                )
    return labels


def test_walk_forward_folds_are_chronological_and_ordered():
    folds = walk_forward_folds("2022-01-01", "2025-01-01")
    assert len(folds) >= 5
    assert folds[0]["fold_id"] == "fold-00"
    for previous, current in zip(folds, folds[1:], strict=False):
        assert previous["validation_end"] <= current["validation_start"]


def test_sealed_holdout_uses_regime_window_and_blocks_further_non_holdout(tmp_path):
    import sqlite3

    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_regime(experiment_db)

    regime_id = draft_evaluation_regime(
        experiment_db,
        "snap-1",
        coverage_start="2023-01-01",
        coverage_end="2024-12-31",
    )

    run_sealed_holdout(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )

    # Regime is now sealed.
    with experiment_db.connect(read_only=True) as conn:
        sealed = conn.execute(
            "SELECT sealed_at FROM evaluation_regime WHERE regime_id=?", (regime_id,)
        ).fetchone()[0]
    assert sealed is not None

    # After sealing, a non-HOLDOUT attempt must be blocked by the trigger.
    with pytest.raises(sqlite3.IntegrityError, match="regime sealed"):
        with experiment_db.connect() as conn:
            conn.execute(
                "INSERT INTO experiment_attempt VALUES "
                "('x1',?,'snap-1','BASELINE','B0','{}','h4',NULL,NULL,1,'COMPLETE',NULL,"
                "'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')",
                (regime_id,),
            )

    # The holdout attempt itself was recorded (COMPLETE or INSUFFICIENT_DATA).
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT variant_kind, status FROM experiment_attempt WHERE regime_id=?",
            (regime_id,),
        ).fetchall()
    assert any(row["variant_kind"] == "HOLDOUT" for row in rows)


def test_re_sealing_blocked_after_holdout(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_regime(experiment_db)
    regime_id = draft_evaluation_regime(
        experiment_db, "snap-1", coverage_start="2023-01-01", coverage_end="2024-12-31"
    )
    run_sealed_holdout(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    with pytest.raises(ValueError, match="already has a sealed HOLDOUT attempt"):
        run_sealed_holdout(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id="snap-1",
            baseline="B3_HUNTERS_ONLY",
            screens=_synthetic_screens(),
            outcome_labels=_synthetic_outcomes(),
        )

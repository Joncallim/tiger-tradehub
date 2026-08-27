from tradehub_research.validation.ablations import (
    record_committee_insufficient_data,
    run_remove_one_hunter_ablations,
)
from tradehub_research.validation.attempt_ledger import attempts_by_status, variant_count
from tradehub_research.validation.baselines import evaluate_baseline
from tradehub_research.validation.experiment_db import ExperimentDB


def _seed_regime(experiment_db):
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot VALUES "
            "('snap-1','abc',11,NULL,'{}','h1','/tmp/x','h2','{}','READY','2025-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO evaluation_regime VALUES "
            "('regime-1','snap-1','{}','h3',NULL,'2025-01-01T00:00:00Z')"
        )


def _synthetic_screens():
    """Two dates x four securities x six families; a modest signal in the
    outcomes that the confidence diagnostic should rank positively."""
    screens = []
    families = (
        "valuation",
        "inflection",
        "quality",
        "informed_activity",
        "event",
        "momentum_confirmation",
    )
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"):
        for sid, confidence in (
            ("a", 0.9),
            ("b", 0.7),
            ("c", 0.5),
            ("d", 0.3),
        ):
            for family in families:
                screens.append(
                    {
                        "screen_result_id": f"{day}-{sid}-{family}",
                        "run_id": f"run-{day}",
                        "security_id": sid,
                        "config_hash": f"cfg-{family}",
                        "family": family,
                        "passed": confidence >= 0.6,
                        "sufficient_data": True,
                        "confidence": confidence,
                        "data_quality": 0.9,
                        "computed_at": f"{day}T00:00:00Z",
                    }
                )
    return screens


def _synthetic_outcomes():
    """Outcomes where higher-confidence securities do better on average."""
    labels = []
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"):
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


def test_evaluate_baseline_records_attempt_and_metrics(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_regime(experiment_db)

    summary = evaluate_baseline(
        experiment_db,
        regime_id="regime-1",
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )

    assert summary["baseline"] == "B3_HUNTERS_ONLY"
    assert "21" in summary["horizons"]
    assert variant_count(experiment_db, "regime-1") == 1
    statuses = attempts_by_status(experiment_db, "regime-1")
    assert statuses == {"COMPLETE": 1}

    with experiment_db.connect(read_only=True) as conn:
        metric_count = conn.execute("SELECT COUNT(*) FROM metric").fetchone()[0]
    assert metric_count >= 1


def test_remove_one_hunter_ablation_truly_removes_component(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_regime(experiment_db)

    results = run_remove_one_hunter_ablations(
        experiment_db,
        regime_id="regime-1",
        dataset_snapshot_id="snap-1",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )

    assert len(results) == 6  # one per Hunter family
    assert variant_count(experiment_db, "regime-1") == 6


def test_committee_ablations_record_insufficient_data_not_silent(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_regime(experiment_db)

    results = record_committee_insufficient_data(
        experiment_db, regime_id="regime-1", dataset_snapshot_id="snap-1"
    )

    assert len(results) == 2
    statuses = attempts_by_status(experiment_db, "regime-1")
    assert statuses == {"INSUFFICIENT_DATA": 2}  # visible, honest, never a pass

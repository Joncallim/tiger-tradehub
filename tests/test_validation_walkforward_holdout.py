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
    """Labels carry benchmark_return (same per date/horizon across
    securities), total_return, and benchmark_relative_return; the benchmark
    return deliberately differs from the equal-weight universe return
    (mean total_return) so B0 and B1 are economically distinct."""
    labels = []
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01", "2024-06-01"):
        for sid, magnitude in (("a", 0.10), ("b", 0.05), ("c", -0.02), ("d", -0.08)):
            for horizon in (21, 63):
                benchmark_return = 0.02 * (horizon / 21)
                relative = magnitude * (horizon / 21)
                labels.append(
                    {
                        "label_id": f"{day}-{sid}-{horizon}",
                        "dataset_snapshot_id": "snap-1",
                        "security_id": sid,
                        "observation_date": day,
                        "horizon_sessions": horizon,
                        "outcome_status": "OBSERVED",
                        "benchmark_return": benchmark_return,
                        "total_return": benchmark_return + relative,
                        "benchmark_relative_return": relative,
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


def test_db_trigger_blocks_second_holdout_attempt_per_regime(tmp_path):
    """The reviewer's probe: even a direct SQL insert of a second HOLDOUT
    attempt for a sealed regime must abort at the DB boundary (the final
    holdout is one-time structurally, not just by application convention)."""
    import sqlite3

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
    with pytest.raises(sqlite3.IntegrityError, match="one final HOLDOUT"):
        with experiment_db.connect() as conn:
            conn.execute(
                "INSERT INTO experiment_attempt VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "second-holdout",
                    regime_id,
                    "snap-1",
                    "HOLDOUT",
                    "HOLDOUT_DUP",
                    "{}",
                    "deadbeef" * 8,
                    None,
                    63,
                    2,
                    "RUNNING",
                    None,
                    "2026-01-01T00:00:00Z",
                    None,
                ),
            )


def _seed_regime_with_holdout_window(
    experiment_db, holdout_start, holdout_end, snapshot_id="snap-1"
):
    """Seed a regime whose holdout window covers the given span -- used by
    the variant-identity tests so the holdout evaluates the SAME dates as
    the development evaluation."""
    import hashlib
    import json
    import uuid

    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dataset_snapshot VALUES "
            f"('{snapshot_id}','abc',11,NULL,'{{}}','h1','/tmp/x','h2','{{}}','READY','2025-01-01T00:00:00Z')"
        )
    spec = {
        "dataset_snapshot_id": snapshot_id,
        "coverage_start": "2024-01-01",
        "coverage_end": "2024-12-31",
        "max_horizon_sessions": 252,
        "fold_months": 6,
        "development_window": {"start": "2024-01-01", "end": holdout_start},
        "holdout_window": {"start": holdout_start, "end": holdout_end},
        "label_maturity_cutoff": "2024-06-30",
        "unmatured_tail": {"start": "2024-06-30", "end": "2024-12-31"},
    }
    spec_json = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    spec_hash = hashlib.sha256(spec_json.encode()).hexdigest()
    regime_id = str(uuid.uuid4())
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO evaluation_regime "
            "(regime_id,dataset_snapshot_id,spec_json,spec_hash,sealed_at,created_at) "
            "VALUES (?,?,?,?,NULL,?)",
            (regime_id, snapshot_id, spec_json, spec_hash, "2025-01-01T00:00:00Z"),
        )
    return regime_id


def test_holdout_variant_identity_matches_development_evaluation(tmp_path):
    """P1 fix: the sealed holdout must evaluate the EXACT frozen variant
    named before sealing -- the same canonical implementation as
    development evaluation. With intentionally DIVERGENT B2/B3/B4 signals
    (the multi-family fixture's families rank differently), each holdout
    variant must reproduce its development counterpart's horizon values
    exactly, and the three holdout variants must differ from each other (a
    variant identifier can never describe one strategy while executing
    another)."""
    from tests.test_validation_baselines_ablations import (
        _synthetic_outcomes as _divergent_outcomes,
    )
    from tests.test_validation_baselines_ablations import (
        _synthetic_screens as _divergent_screens,
    )
    from tradehub_research.validation.baselines import evaluate_baseline

    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    screens = _divergent_screens()
    outcomes = _divergent_outcomes()

    holdout_ics = {}
    for baseline in ("B2_FACTOR_COMPOSITE", "B3_HUNTERS_ONLY", "B4_EQUAL_SCORING"):
        regime_id = _seed_regime_with_holdout_window(experiment_db, "2024-01-01", "2024-04-30")
        # Development evaluation (BASELINE kind, before sealing).
        development = evaluate_baseline(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id="snap-1",
            baseline=baseline,
            screens=screens,
            outcome_labels=outcomes,
        )
        # Sealed holdout (HOLDOUT kind, same canonical implementation).
        holdout = run_sealed_holdout(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id="snap-1",
            baseline=baseline,
            screens=screens,
            outcome_labels=outcomes,
        )
        # Identity: every horizon value reproduced exactly.
        assert holdout["horizons"] == development["horizons"], (
            f"holdout {baseline} diverged from development evaluation"
        )
        holdout_ics[baseline] = holdout["horizons"]["21"].get("mean_ic")

    # Hostile divergence: the three holdout variants are NOT the same
    # strategy -- their ICs must differ.
    assert len(set(holdout_ics.values())) == 3


def test_holdout_b0_b1_are_economically_distinct(tmp_path):
    """P1 fix: B0 holdout = pinned benchmark return itself; B1 holdout =
    equal-weight universe return. Identical dates/cost conventions, but
    genuinely distinct series -- B0 result != B1 result by VALUE."""
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    screens = _synthetic_screens()
    outcomes = _synthetic_outcomes()

    results = {}
    for baseline in ("B0_BENCHMARK", "B1_UNIVERSE"):
        regime_id = _seed_regime_with_holdout_window(experiment_db, "2024-01-01", "2024-06-30")
        results[baseline] = run_sealed_holdout(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id="snap-1",
            baseline=baseline,
            screens=screens,
            outcome_labels=outcomes,
        )

    b0 = results["B0_BENCHMARK"]["horizons"]["21"]["mean_return"]
    b1 = results["B1_UNIVERSE"]["horizons"]["21"]["mean_return"]
    assert b0 != b1
    assert b0 == pytest.approx(0.02)  # benchmark return itself
    assert b1 == pytest.approx(0.02 + (0.10 + 0.05 - 0.02 - 0.08) / 4)

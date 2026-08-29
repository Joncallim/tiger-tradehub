import pytest

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
    """Two dates x four securities x six families.

    Two families (valuation, inflection) rank securities in agreement with
    the outcome (a best, d worst); four families rank OPPOSITE. This makes
    the B2 family-rank composite (rank-average: majority anti order) and
    the B4 mean-confidence signal (mean: minority agree order... by
    construction they genuinely disagree), so the five baselines are
    distinct and every family removal changes the evaluated signal."""
    screens = []
    families = (
        "valuation",
        "inflection",
        "quality",
        "informed_activity",
        "event",
        "momentum_confirmation",
    )
    # Confidence per security per family, on a scale of 0..1, chosen so
    # that EVERY baseline signal has cross-sectional variance:
    #   B3 any-pass: a/b pass (agreeing), c fails all, d passes (opposing)
    #   B4 mean: a > c > b = d (ties, but variance exists)
    #   B2 composite: rank-average gives a > b > c > d (opposite order)
    agreeing = {"a": 0.9, "b": 0.6, "c": 0.5, "d": 0.2}  # outcome-agreeing
    opposing = {"a": 0.4, "b": 0.45, "c": 0.55, "d": 0.65}  # outcome-opposing
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"):
        for family_index, family in enumerate(families):
            table = agreeing if family_index < 2 else opposing
            for sid in ("a", "b", "c", "d"):
                confidence = table[sid]
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
    """Outcomes where higher-confidence securities do better on average.

    Each label carries benchmark_return (the pinned benchmark return for
    the date/horizon -- the SAME value for every security on a date, per
    the benchmark-artifact contract), total_return, and
    benchmark_relative_return. benchmark_return is deliberately NOT equal
    to the equal-weight universe return (mean total_return), so B0 and B1
    are economically distinct series (B0 != B1 regression)."""
    labels = []
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"):
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

    full = evaluate_baseline(
        experiment_db,
        regime_id="regime-1",
        dataset_snapshot_id="snap-1",
        baseline="B4_EQUAL_SCORING",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    results = run_remove_one_hunter_ablations(
        experiment_db,
        regime_id="regime-1",
        dataset_snapshot_id="snap-1",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )

    assert len(results) == 6  # one per Hunter family
    assert variant_count(experiment_db, "regime-1") == 7  # full + six removals

    # The ablation must TRULY remove the component: with the equal-scoring
    # signal (mean confidence across families), removing any family changes
    # every security's signal, so no ablation result may equal the full
    # population's IC at every horizon (the reviewer's P1 probe).
    full_ic = full["horizons"]["21"].get("mean_ic")
    differing = [r for r in results if r["horizons"]["21"].get("mean_ic") != full_ic]
    assert len(differing) == 6


def test_baselines_are_genuinely_distinct(tmp_path):
    """B0-B4 must be distinct evaluations (the reviewer's P1 probes: they
    collapsed into one confidence-based signal, and B0/B1 both consumed
    benchmark_relative_return). B0 = pinned benchmark return itself;
    B1 = equal-weight universe return; B2-B4 = distinct IC baselines.
    With the fixture, benchmark_return (0.02) != equal-weight universe
    return (0.02 + mean relative), so B0 result != B1 result by VALUE."""
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_regime(experiment_db)

    summaries = {}
    for baseline in (
        "B0_BENCHMARK",
        "B1_UNIVERSE",
        "B2_FACTOR_COMPOSITE",
        "B3_HUNTERS_ONLY",
        "B4_EQUAL_SCORING",
    ):
        summaries[baseline] = evaluate_baseline(
            experiment_db,
            regime_id="regime-1",
            dataset_snapshot_id="snap-1",
            baseline=baseline,
            screens=_synthetic_screens(),
            outcome_labels=_synthetic_outcomes(),
        )

    # B0/B1 are portfolio-style: they report mean_return, not mean_ic.
    assert "mean_return" in summaries["B0_BENCHMARK"]["horizons"]["21"]
    assert "mean_return" in summaries["B1_UNIVERSE"]["horizons"]["21"]
    assert "mean_ic" in summaries["B2_FACTOR_COMPOSITE"]["horizons"]["21"]
    assert "mean_ic" in summaries["B3_HUNTERS_ONLY"]["horizons"]["21"]
    assert "mean_ic" in summaries["B4_EQUAL_SCORING"]["horizons"]["21"]

    # B0 != B1 BY VALUE: B0 is the benchmark return itself (0.02 at h21);
    # B1 is the equal-weight universe return (0.02 + mean relative =
    # 0.0325 at h21). Merely having a mean_return field is not enough.
    b0_h21 = summaries["B0_BENCHMARK"]["horizons"]["21"]["mean_return"]
    b1_h21 = summaries["B1_UNIVERSE"]["horizons"]["21"]["mean_return"]
    assert b0_h21 != b1_h21
    assert b0_h21 == pytest.approx(0.02)
    assert b1_h21 == pytest.approx(0.02 + (0.10 + 0.05 - 0.02 - 0.08) / 4)

    # No two IC baselines share identical IC values at every horizon.
    ic_by_baseline = {
        name: [s["horizons"][str(h)].get("mean_ic") for h in (21, 63, 126, 252)]
        for name, s in summaries.items()
        if name != "B0_BENCHMARK" and name != "B1_UNIVERSE"
    }
    assert len({tuple(v) for v in ic_by_baseline.values()}) == 3


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

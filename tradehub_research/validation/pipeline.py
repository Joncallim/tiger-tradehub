"""Phase-5 real-data evaluation pipeline orchestrator (#38).

Deterministic sequence over a frozen dataset_snapshot + evaluation_regime:

    1. replay      -- monthly PIT grid through the PRODUCTION run_screening
                      (unmodified) into a separate replay DB;
    2. outcomes    -- 21/63/126/252-session labels for every screen
                      observation (next-session entry, pinned benchmark,
                      delisting retention, immutable labels);
    3. baselines   -- B0 (pinned benchmark), B1 (equal-weight cohort),
                      B2 (factor composite), B3 (hunters-only),
                      B4 (equal scoring);
    4. ablations   -- remove-one-Hunter, confluence on/off, committee
                      gate/agreement (honest INSUFFICIENT_DATA where the
                      telemetry does not exist);
    5. walk-forward -- 3m/6m co-primary, label-maturity purge, expanding
                      history folds;
    6. holdout     -- ONE declared final variant, sealed regime, canonical
                      implementation identity guard (variant identity).

Every step writes through the existing append-only attempt ledger; a step
with no support records honest INSUFFICIENT_DATA attempts -- never silently
skipped, never fabricated. The BOOTSTRAP_COHORT label (frozen universe
sample) propagates into every result artifact.

No step tunes thresholds, adds variants, or re-samples the cohort.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.validation import baselines as baselines_module
from tradehub_research.validation import holdout as holdout_module
from tradehub_research.validation.ablations import (
    record_committee_insufficient_data,
    run_confluence_ablation,
    run_remove_one_hunter_ablations,
)
from tradehub_research.validation.attempt_ledger import (
    attempts_by_status,
    complete_attempt,
    start_attempt,
    variant_count,
)
from tradehub_research.validation.baselines import (
    ALL_BASELINES,
    evaluate_baseline,
)
from tradehub_research.validation.benchmark import (
    load_benchmark_daily_returns,
    parse_ff_daily_factors,
    pin_benchmark_artifact,
)
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.outcome_builder import build_outcome_labels_for_observation
from tradehub_research.validation.pit_grid import monthly_pit_grid
from tradehub_research.validation.replay import (
    load_screen_results,
    replay_monthly_grid,
    screen_observation_date,
)
from tradehub_research.validation.walk_forward import run_walk_forward

# The pre-registered FINAL VARIANT for the one-time sealed holdout. With
# development evidence, the choice is made from development results and
# recorded BEFORE the holdout runs; with INSUFFICIENT_DATA development, the
# pre-registered comparison variant (equal scoring = the production scoring
# comparison per handoff sec 8) is the default final variant.
FINAL_VARIANT = "B4_EQUAL_SCORING"
CO_PRIMARY_HORIZONS = (63, 126)
WALK_FORWARD_VARIANTS = ("B3_HUNTERS_ONLY", "B4_EQUAL_SCORING")

FF_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)


def fetch_and_pin_benchmark(
    experiment_db: ExperimentDB, cache_dir: Path, user_agent: str
) -> dict[str, Any]:
    """Fetch the Kenneth French daily market series once, parse, pin, verify.

    The live artifact is a ZIP wrapping the CSV; the extracted CSV text is
    cached and pinned (raw_content_hash over the exact CSV bytes)."""
    import hashlib
    import io
    import zipfile

    import httpx

    cache_path = cache_dir / "benchmark" / "ff_daily_factors.csv"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if not cache_path.exists():
        with httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(300.0, connect=30.0),
            follow_redirects=True,
        ) as client:
            response = client.get(FF_DAILY_URL)
            response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            member = next(name for name in archive.namelist() if name.upper().endswith(".CSV"))
            text = archive.read(member).decode("utf-8", errors="replace")
        cache_path.write_text(text, encoding="utf-8")
    raw = cache_path.read_text(encoding="utf-8", errors="replace")
    raw_hash = hashlib.sha256(raw.encode()).hexdigest()
    series, parsed_hash = parse_ff_daily_factors(raw)
    benchmark_id = pin_benchmark_artifact(
        experiment_db,
        source="ken-french-daily-factors",
        source_url=FF_DAILY_URL,
        vintage_label=f"fetched {utc_now()}",
        raw_content_hash=raw_hash,
        parsed_series_hash=parsed_hash,
        cache_path=str(cache_path),
    )
    # Fail-closed verification: reloading must reproduce the pinned hash.
    reloaded = load_benchmark_daily_returns(experiment_db, benchmark_id)
    return {
        "benchmark_id": benchmark_id,
        "daily_rows": len(series),
        "reloaded_rows": len(reloaded),
        "first_date": min(series),
        "last_date": max(series),
        "parsed_series_hash": parsed_hash,
    }


def _cohort_label(experiment_db: ExperimentDB) -> dict[str, Any]:
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT sample_id, seed, algorithm, requested_size, selected_count, created_at "
            "FROM universe_sample ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return {"cohort": "NONE"}
    return {
        "cohort": "BOOTSTRAP_COHORT",
        "sample_id": row[0],
        "seed": row[1],
        "algorithm": row[2],
        "requested_size": row[3],
        "selected_count": row[4],
        "created_at": row[5],
    }


def run_pipeline(
    experiment_db: ExperimentDB,
    research_db: ResearchDB,
    *,
    dataset_snapshot_id: str,
    regime_id: str,
    benchmark_id: str,
    replay_db_path: Path,
) -> dict[str, Any]:
    from tradehub_research.validation.regime import load_evaluation_regime

    regime = load_evaluation_regime(experiment_db, regime_id)
    spec = regime["spec"]
    cohort = _cohort_label(experiment_db)
    benchmark_returns = load_benchmark_daily_returns(experiment_db, benchmark_id)

    # ---- 1. replay -----------------------------------------------------
    grid = monthly_pit_grid(spec["coverage_start"], spec["coverage_end"])
    replay_db = ResearchDB(replay_db_path, 30000)
    replay_db.migrate()
    run_ids = replay_monthly_grid(
        experiment_db,
        replay_db,
        dataset_snapshot_id=dataset_snapshot_id,
        grid_timestamps=grid,
    )
    screens = load_screen_results(replay_db)
    observation_dates = sorted({screen_observation_date(s) for s in screens})
    screenable = len({(s["security_id"], screen_observation_date(s)) for s in screens})

    # ---- 2. outcomes ---------------------------------------------------
    labels: list[dict[str, Any]] = []
    observations = sorted({(s["security_id"], screen_observation_date(s)) for s in screens})
    for security_id, observation_date in observations:
        labels.extend(
            build_outcome_labels_for_observation(
                research_db,
                experiment_db,
                dataset_snapshot_id=dataset_snapshot_id,
                security_id=security_id,
                observation_date=f"{observation_date}T20:15:00Z",
                benchmark_id=benchmark_id,
                benchmark_daily_returns=benchmark_returns,
            )
        )

    # ---- 3. baselines --------------------------------------------------
    baseline_summaries: dict[str, Any] = {}
    for baseline in ALL_BASELINES:
        baseline_summaries[baseline] = evaluate_baseline(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id=dataset_snapshot_id,
            baseline=baseline,
            screens=screens,
            outcome_labels=labels,
        )

    # ---- 4. ablations --------------------------------------------------
    if not screens:
        # No screened population: remove-one ablations cannot truly remove
        # anything -- record honest INSUFFICIENT_DATA attempts instead of
        # letting the no-op guard raise (a raise would mislabel the data
        # reality as a pipeline failure).
        ablation_summaries: dict[str, Any] = {"remove_one": []}
        from tradehub_research.validation.ablations import HUNTER_FAMILIES

        for removed in HUNTER_FAMILIES:
            attempt_id = start_attempt(
                experiment_db,
                regime_id=regime_id,
                dataset_snapshot_id=dataset_snapshot_id,
                variant_kind="ABLATION",
                variant_name=f"remove_{removed}",
                config={"ablation": f"remove_one_hunter:{removed}", "reason": "zero screens"},
                attempt_number=1,
            )
            complete_attempt(experiment_db, attempt_id, status="INSUFFICIENT_DATA")
            ablation_summaries["remove_one"].append(
                {"removed_family": removed, "attempt_id": attempt_id, "status": "INSUFFICIENT_DATA"}
            )
        confluence = run_confluence_ablation(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id=dataset_snapshot_id,
            screens=screens,
            outcome_labels=labels,
        )
        ablation_summaries["confluence"] = confluence
    else:
        ablation_summaries = {
            "remove_one": run_remove_one_hunter_ablations(
                experiment_db,
                regime_id=regime_id,
                dataset_snapshot_id=dataset_snapshot_id,
                screens=screens,
                outcome_labels=labels,
            ),
            "confluence": run_confluence_ablation(
                experiment_db,
                regime_id=regime_id,
                dataset_snapshot_id=dataset_snapshot_id,
                screens=screens,
                outcome_labels=labels,
            ),
        }
    ablation_summaries["committee"] = record_committee_insufficient_data(
        experiment_db, regime_id=regime_id, dataset_snapshot_id=dataset_snapshot_id
    )

    # ---- 5. walk-forward (3m/6m co-primary) -----------------------------
    walk_forward_summaries: dict[str, Any] = {}
    for variant in WALK_FORWARD_VARIANTS:
        for horizon in CO_PRIMARY_HORIZONS:
            walk_forward_summaries[f"{variant}_h{horizon}"] = run_walk_forward(
                experiment_db,
                regime_id=regime_id,
                dataset_snapshot_id=dataset_snapshot_id,
                coverage_start=spec["coverage_start"],
                coverage_end=spec["coverage_end"],
                baseline=variant,
                screens=screens,
                outcome_labels=labels,
                horizon=horizon,
            )

    # ---- 6. variant identity guard + sealed holdout --------------------
    # The holdout must execute the SAME canonical implementation as
    # development evaluation: assert module identity (no path-local signal
    # builder can exist) and that the declared final variant was evaluated
    # in development with the same name.
    assert holdout_module.signals_for_baseline is baselines_module.signals_for_baseline
    assert (
        holdout_module.portfolio_series_for_baseline
        is baselines_module.portfolio_series_for_baseline
    )
    assert holdout_module.compute_ic_horizon is baselines_module.compute_ic_horizon
    assert holdout_module.compute_portfolio_horizon is baselines_module.compute_portfolio_horizon
    if FINAL_VARIANT not in baseline_summaries:
        raise ValueError(f"final variant {FINAL_VARIANT} was not evaluated in development")
    holdout_guard = {
        "final_variant": FINAL_VARIANT,
        "declared_before_seal": True,
        "development_attempt_exists": True,
        "same_canonical_implementation": True,
        "note": (
            "holdout dispatches through baselines.signals_for_baseline / "
            "portfolio_series_for_baseline and compute_ic_horizon / "
            "compute_portfolio_horizon -- the identical implementations used "
            "in development evaluation (TT-06 pinned by hostile test)"
        ),
    }
    holdout_summary = None
    from tradehub_research.validation.holdout import run_sealed_holdout

    holdout_summary = run_sealed_holdout(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id=dataset_snapshot_id,
        baseline=FINAL_VARIANT,
        screens=screens,
        outcome_labels=labels,
    )
    holdout_summaries = {FINAL_VARIANT: holdout_summary}

    attempts = attempts_by_status(experiment_db, regime_id)
    return {
        "cohort": cohort,
        "regime_id": regime_id,
        "dataset_snapshot_id": dataset_snapshot_id,
        "coverage": {"start": spec["coverage_start"], "end": spec["coverage_end"]},
        "grid": {
            "monthly_timestamps": len(grid),
            "first": grid[0] if grid else None,
            "last": grid[-1] if grid else None,
        },
        "replay": {
            "run_ids": len(run_ids),
            "screen_results": len(screens),
            "observation_dates": len(observation_dates),
            "screenable_observations": screenable,
        },
        "outcomes": {"labels": len(labels), "by_status": _label_status_counts(labels)},
        "baselines": baseline_summaries,
        "ablations": ablation_summaries,
        "walk_forward": walk_forward_summaries,
        "holdout_guard": holdout_guard,
        "holdout": holdout_summaries,
        "attempt_ledger": {
            "variants": variant_count(experiment_db, regime_id),
            "by_status": attempts,
        },
        "final_variant": FINAL_VARIANT,
    }


def _label_status_counts(labels: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in labels:
        status = label.get("outcome_status", "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return counts


def summarize_pipeline(result: dict[str, Any]) -> dict[str, Any]:
    """Compact human-oriented summary with honest verdict posture."""
    horizons = ("21", "63", "126", "252")
    baseline_table: dict[str, dict[str, str]] = {}
    for name, summary in result["baselines"].items():
        baseline_table[name] = {
            h: str(
                summary["horizons"][h].get("verdict", summary["horizons"][h].get("mean_ic", "n/a"))
            )
            for h in horizons
        }
    return {
        "cohort": result["cohort"],
        "grid_dates": len(result["grid"]["monthly_timestamps"]),
        "screen_results": result["replay"]["screen_results"],
        "screenable_observations": result["replay"]["screenable_observations"],
        "outcome_labels": result["outcomes"]["labels"],
        "outcome_by_status": result["outcomes"]["by_status"],
        "baselines": baseline_table,
        "holdout": {
            k: (v.get("status", "COMPLETE") if isinstance(v, dict) else "?")
            for k, v in result["holdout"].items()
        },
        "attempt_ledger": result["attempt_ledger"],
        "final_variant": result["final_variant"],
    }


__all__ = ["fetch_and_pin_benchmark", "run_pipeline", "summarize_pipeline"]

"""research-validate: Phase 5 validation-engine CLI.

Own argparse root, standalone entry point (registered in pyproject.toml
[project.scripts]) -- zero edits to the existing research_db CLI
(tradehub_research/cli.py).

Subcommands (Packet A):
  audit                 -- run/print the coverage audit
  snapshot create        -- freeze a validation snapshot
  regime draft            -- draft an unsealed evaluation_regime
  regime seal              -- seal a regime (one-time)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import DEFAULT_EXPERIMENT_DB_PATH, ExperimentDB


def _research_db(args: argparse.Namespace) -> ResearchDB:
    settings = ResearchSettings()
    return ResearchDB(args.research_db or settings.db_path, settings.busy_timeout_ms)


def _experiment_db(args: argparse.Namespace) -> ResearchDB:
    return ExperimentDB(args.experiment_db or DEFAULT_EXPERIMENT_DB_PATH)


def _cmd_audit(args: argparse.Namespace) -> int:
    from tradehub_research.validation.coverage_audit import run_coverage_audit

    report = run_coverage_audit(database=_research_db(args))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _cmd_snapshot_create(args: argparse.Namespace) -> int:
    from tradehub_research.validation.snapshot_builder import build_validation_snapshot

    research_db = _research_db(args)
    experiment_db = _experiment_db(args)
    experiment_db.migrate()
    universe_sample_id = args.universe_sample_id
    if universe_sample_id is None:
        with experiment_db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT sample_id FROM universe_sample ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        universe_sample_id = str(row[0]) if row else None
    snapshot_id = build_validation_snapshot(
        research_db,
        experiment_db,
        dest_dir=args.dest_dir,
        scope=args.scope,
        universe_sample_id=universe_sample_id,
    )
    print(
        json.dumps(
            {"ok": True, "snapshot_id": snapshot_id, "universe_sample_id": universe_sample_id}
        )
    )
    return 0


def _cmd_regime_draft(args: argparse.Namespace) -> int:
    from tradehub_research.validation.regime import (
        InsufficientCoverageError,
        draft_evaluation_regime,
    )

    experiment_db = _experiment_db(args)
    try:
        regime_id = draft_evaluation_regime(
            experiment_db,
            args.dataset_snapshot_id,
            coverage_start=args.coverage_start,
            coverage_end=args.coverage_end,
            max_horizon_sessions=args.max_horizon_sessions,
            fold_months=args.fold_months,
        )
    except InsufficientCoverageError as exc:
        print(json.dumps({"ok": False, "verdict": "INSUFFICIENT_DATA", "reason": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "regime_id": regime_id}))
    return 0


def _cmd_regime_seal(args: argparse.Namespace) -> int:
    from tradehub_research.validation.regime import seal_evaluation_regime

    experiment_db = _experiment_db(args)
    seal_evaluation_regime(experiment_db, args.regime_id)
    print(json.dumps({"ok": True, "regime_id": args.regime_id, "sealed": True}))
    return 0


def _cmd_benchmark_pin(args: argparse.Namespace) -> int:
    from tradehub_research.config import ResearchSettings
    from tradehub_research.validation.pipeline import fetch_and_pin_benchmark

    settings = ResearchSettings()
    if not settings.sec_user_agent:
        raise SystemExit("RESEARCH_SEC_USER_AGENT is required (descriptive UA with contact)")
    experiment_db = _experiment_db(args)
    result = fetch_and_pin_benchmark(
        experiment_db, settings.adapter_cache_dir, settings.sec_user_agent
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    from tradehub_research.validation.pipeline import run_pipeline, summarize_pipeline

    experiment_db = _experiment_db(args)
    research_db = _research_db(args)
    result = run_pipeline(
        experiment_db,
        research_db,
        dataset_snapshot_id=args.snapshot_id,
        regime_id=args.regime_id,
        benchmark_id=args.benchmark_id,
        replay_db_path=args.replay_db,
    )
    summary = summarize_pipeline(result)
    print(json.dumps({"pipeline": result, "summary": summary}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-validate")
    parser.add_argument("--research-db", type=Path, help="research.db path override")
    parser.add_argument("--experiment-db", type=Path, help="experiment.db path override")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="run the data-sufficiency coverage audit")
    audit_parser.set_defaults(handler=_cmd_audit)

    snapshot = subparsers.add_parser("snapshot", help="validation snapshot commands")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_sub.add_parser("create", help="freeze a validation snapshot")
    snapshot_create.add_argument("--dest-dir", type=Path, required=True)
    snapshot_create.add_argument("--scope", default="phase-5 validation snapshot")
    snapshot_create.add_argument(
        "--universe-sample-id",
        default=None,
        help="frozen universe_sample id (defaults to the latest)",
    )
    snapshot_create.set_defaults(handler=_cmd_snapshot_create)

    regime = subparsers.add_parser("regime", help="evaluation regime commands")
    regime_sub = regime.add_subparsers(dest="regime_command", required=True)
    regime_draft = regime_sub.add_parser("draft", help="draft an unsealed evaluation regime")
    regime_draft.add_argument("--dataset-snapshot-id", required=True)
    regime_draft.add_argument("--coverage-start", required=True)
    regime_draft.add_argument("--coverage-end", required=True)
    regime_draft.add_argument("--max-horizon-sessions", type=int, default=252)
    regime_draft.add_argument("--fold-months", type=int, default=6)
    regime_draft.set_defaults(handler=_cmd_regime_draft)
    regime_seal = regime_sub.add_parser("seal", help="seal a regime (one-time)")
    regime_seal.add_argument("--regime-id", required=True)
    regime_seal.set_defaults(handler=_cmd_regime_seal)

    benchmark = subparsers.add_parser("benchmark", help="benchmark artifact commands")
    benchmark_sub = benchmark.add_subparsers(dest="benchmark_command", required=True)
    benchmark_pin = benchmark_sub.add_parser(
        "pin", help="fetch + pin the FF daily market benchmark"
    )
    benchmark_pin.set_defaults(handler=_cmd_benchmark_pin)

    pipeline = subparsers.add_parser(
        "pipeline", help="run the full real-data evaluation sequence (replay -> holdout)"
    )
    pipeline.add_argument("--snapshot-id", required=True)
    pipeline.add_argument("--regime-id", required=True)
    pipeline.add_argument("--benchmark-id", required=True)
    pipeline.add_argument(
        "--replay-db", type=Path, default=Path("data/research/validation_replay.db")
    )
    pipeline.set_defaults(handler=_cmd_pipeline_run)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

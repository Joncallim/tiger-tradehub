#!/usr/bin/env python3
"""Operator CLI for TradeHub deployment provenance.

Answers, deterministically and from committed source:

    python deploy/deployment_cli.py verify   # what version is running right now?
    python deploy/deployment_cli.py record   # stamp the manifest for the live revision
    python deploy/deployment_cli.py plan     # show the rollback target without acting
    python deploy/deployment_cli.py rollback --confirm

``verify`` exits non-zero unless the recorded manifest is valid, HEAD is exactly
the recorded ``deployed_commit``, the working tree matches the recorded
``deployed_tree``, there is no genuine drift, and the recorded rollback target
exists in this clone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deployment_provenance import (  # noqa: E402
    DEFAULT_HOST_LOCAL_CONFIG,
    DeploymentError,
    DriftError,
    ManifestError,
    classify_drift,
    live_revision,
    manifest_path,
    perform_rollback,
    plan_rollback,
    read_manifest,
    record,
    verify,
)

DEFAULT_REPO = Path("/opt/tiger-tradehub")


def _default_repo() -> Path:
    if (DEFAULT_REPO / ".git").exists():
        return DEFAULT_REPO
    return Path(__file__).resolve().parents[1]


def _print_report(report: dict) -> None:
    live = report.get("live") or {}
    print(f"repo            : {report['repo']}")
    print(f"manifest        : {report['manifest_path']}")
    if live:
        where = live["branch"] if not live["detached"] else "(detached HEAD)"
        print(f"checked-out HEAD: {live['commit']} tree={live['tree'][:12]} {where}")
        print(f"running subject : {live['subject']}")
    print(
        "manifest state  : "
        f"present={report['manifest_present']} valid={report['manifest_valid']} "
        f"head_matches={report['deployed_commit_matches_head']} "
        f"tree_matches={report['tree_matches_manifest']}"
    )
    drift = report.get("drift")
    if drift:
        print(f"genuine drift   : {drift['genuine_drift'] or 'none'}")
        print(f"host-local      : {drift['host_local'] or 'none'}")
        print(f"generated       : {len(drift['generated_runtime'])} path(s) (not drift)")
    print(f"rollback target : {report['rollback_target']} ({report['rollback_detail']})")
    for entry in report["host_local_config"]:
        print(f"config          : {entry['path']} present={entry['present']}")
    for error in report["errors"]:
        print(f"error           : {error}")
    for failure in report["failures"]:
        print(f"PROBLEM         : {failure}")
    print(f"RESULT          : {'OK' if report['ok'] else 'FAIL'}")


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify(args.repo, path=args.manifest, require_rollback=not args.no_rollback_required)
    payload = report.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_report(payload)
    return 0 if report.ok else 1


def cmd_record(args: argparse.Namespace) -> int:
    previous: str | None | str = args.previous_commit
    if previous == "none":
        previous = None
    try:
        manifest = record(
            args.repo,
            deployed_at=args.deployed_at,
            source_branch=args.source_branch,
            source_release=args.source_release,
            previous_commit=previous,
            equivalent_commits=args.equivalent or [],
            host_local_config=args.host_local_config,
            allow_dirty=args.allow_dirty,
            force=args.force,
        )
    except DriftError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except DeploymentError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print(f"recorded {manifest_path(args.repo)}")
        print(f"  deployed_commit : {manifest['deployed_commit']}")
        print(f"  deployed_tree   : {manifest['deployed_tree']}")
        print(f"  deployed_at     : {manifest['deployed_at']}")
        print(f"  source_branch   : {manifest['source_branch']}")
        print(f"  previous_commit : {manifest['previous_commit']} (rollback target)")
        print(f"  tree_state      : {manifest['tree_state']}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan = plan_rollback(args.repo, to=args.to)
    except (ManifestError, DeploymentError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(plan, indent=2, sort_keys=True))
    else:
        print(f"rollback: {plan['from_commit']} -> {plan['to_commit']}")
        print(f"  target subject  : {plan['to_subject']}")
        print(f"  target tree     : {plan['to_tree']}")
        print(f"  changes content : {plan['changes_content']}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    try:
        result = perform_rollback(
            args.repo, to=args.to, confirm=args.confirm, restart=not args.no_restart
        )
    except (ManifestError, DeploymentError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    else:
        print(f"rolled back {result['rolled_back_from']} -> {result['rolled_back_to']}")
        print(f"  restarted : {result['restarted']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """One-line answer to 'what version is TradeHub running right now?'"""
    repo = args.repo
    try:
        live = live_revision(repo)
    except DeploymentError as exc:
        print(f"TradeHub revision UNKNOWN: {exc}")
        return 1
    try:
        manifest = read_manifest(repo)
        recorded = manifest["deployed_commit"]
        suffix = "" if recorded == live.commit else f" (manifest records {recorded[:12]})"
        when = manifest.get("deployed_at", "?")
        print(f"TradeHub is running {live.commit[:12]} ({live.subject}) deployed {when}{suffix}")
        return 0 if recorded == live.commit else 1
    except ManifestError as exc:
        print(f"TradeHub is running {live.commit[:12]} ({live.subject}) but {exc}")
        return 1


def cmd_drift(args: argparse.Namespace) -> int:
    drift = classify_drift(args.repo)
    payload = drift.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"genuine drift : {payload['genuine_drift'] or 'none'}")
        print(f"host-local    : {payload['host_local'] or 'none'}")
        print(f"generated     : {payload['generated_runtime'] or 'none'}")
        if payload["legacy_marker_present"]:
            print("WARNING       : superseded DEPLOYED_COMMIT marker still present")
    return 0 if drift.clean else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=None, help="deployed checkout")
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="prove the deployed revision and drift state")
    p_verify.add_argument("--json", action="store_true")
    p_verify.add_argument("--manifest", type=Path, default=None)
    p_verify.add_argument("--no-rollback-required", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_record = sub.add_parser("record", help="write the manifest for the live revision")
    p_record.add_argument("--json", action="store_true")
    p_record.add_argument("--deployed-at", default=None)
    p_record.add_argument("--source-branch", default=None)
    p_record.add_argument("--source-release", default=None)
    p_record.add_argument(
        "--previous-commit",
        default="inherit",
        help="rollback target: 'inherit' (from the existing manifest), 'none', or a SHA",
    )
    p_record.add_argument("--equivalent", action="append", default=None)
    p_record.add_argument(
        "--host-local-config",
        action="append",
        default=None,
        help=f"declared host-local config path (default: {', '.join(DEFAULT_HOST_LOCAL_CONFIG)})",
    )
    p_record.add_argument("--allow-dirty", action="store_true")
    p_record.add_argument("--force", action="store_true")
    p_record.set_defaults(func=cmd_record)

    p_plan = sub.add_parser("plan", help="show the resolved rollback target")
    p_plan.add_argument("--to", default=None)
    p_plan.add_argument("--json", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    p_rollback = sub.add_parser("rollback", help="roll back to the recorded target")
    p_rollback.add_argument("--to", default=None)
    p_rollback.add_argument("--confirm", action="store_true")
    p_rollback.add_argument("--no-restart", action="store_true")
    p_rollback.add_argument("--json", action="store_true")
    p_rollback.set_defaults(func=cmd_rollback)

    p_status = sub.add_parser("status", help="one-line 'what is running' answer")
    p_status.set_defaults(func=cmd_status)

    p_drift = sub.add_parser("drift", help="classify the working tree")
    p_drift.add_argument("--json", action="store_true")
    p_drift.set_defaults(func=cmd_drift)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repo is None:
        args.repo = _default_repo()
    if args.command == "record" and args.host_local_config is None:
        args.host_local_config = list(DEFAULT_HOST_LOCAL_CONFIG)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

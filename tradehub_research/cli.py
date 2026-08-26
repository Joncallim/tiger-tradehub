from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def _database(args: argparse.Namespace) -> ResearchDB:
    settings = ResearchSettings()
    return ResearchDB(args.db or settings.db_path, settings.busy_timeout_ms)


def _cmd_init(args: argparse.Namespace) -> int:
    database = _database(args)
    version = database.migrate()
    print(json.dumps({"ok": True, "schema_version": version, "path": str(database.path)}))
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    database = _database(args)
    result = database.check()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


def _cmd_ingest(args: argparse.Namespace) -> int:
    from tradehub_research.adapters.base import FetchResult, ingest_records
    from tradehub_research.adapters.sec import SecAdapter
    from tradehub_research.adapters.tiingo import TiingoEodAdapter
    from tradehub_research.evidence import EvidenceStore

    database = _database(args)
    settings = ResearchSettings()
    if not args.provider or not args.fixture or not args.kind:
        raise SystemExit("ingest requires --provider, --kind, and --fixture")
    raw = args.fixture.read_bytes()
    fetched = FetchResult(
        args.fixture.resolve().as_uri(), "2099-01-01T00:00:00Z", 200, {}, raw, args.fixture
    )
    if args.provider == "sec":
        if not settings.sec_user_agent:
            raise SystemExit("SEC ingestion requires RESEARCH_SEC_USER_AGENT")
        adapter = SecAdapter(
            user_agent=settings.sec_user_agent, cache_dir=settings.adapter_cache_dir
        )
        if args.kind == "index":
            records = adapter.parse_daily_index(raw, fetched)
            if args.security_id:
                index_dates = sorted({r.structured_fields["filed"] for r in records})
                if not index_dates and args.filed:
                    index_dates = [args.filed]
                records.extend(
                    marker
                    for index_date in index_dates
                    for marker in adapter.index_completeness_records(
                        fetched, index_date=index_date, security_ids=[args.security_id]
                    )
                )
        elif args.kind == "companyfacts":
            records = adapter.parse_companyfacts(raw, fetched)
        elif args.kind == "form4" and args.accession and args.filed:
            predecessor_keys: set[str] = set()
            if args.supersedes_accession:
                with database.connect(read_only=True) as db:
                    predecessor_keys = {
                        str(row[0]).rsplit(":tx:", 1)[1]
                        for row in db.execute(
                            "SELECT source_record_id FROM evidence_event "
                            "WHERE source_id='sec_form4' AND source_record_id LIKE ?",
                            (f"{args.supersedes_accession}:tx:%",),
                        )
                    }
            records = adapter.parse_form4(
                raw,
                fetched,
                accession=args.accession,
                filed=args.filed,
                acceptance_time=args.acceptance_time,
                supersedes_accession=args.supersedes_accession,
                supersedes_transaction_keys=predecessor_keys,
            )
        else:
            raise SystemExit(
                "SEC kind must be index/companyfacts or form4 with --accession/--filed"
            )
        if not args.security_id:
            raise SystemExit("SEC fixture ingestion requires canonical --security-id")
        records = adapter.with_security(records, args.security_id)
    else:
        if args.kind != "eod" or not args.ticker:
            raise SystemExit("Tiingo ingestion requires --kind eod and --ticker")
        adapter = TiingoEodAdapter(
            token=settings.tiingo_token,
            license_confirmed=settings.tiingo_license_confirmed,
            user_agent="TigerTradeHub research-ingest",
            cache_dir=settings.adapter_cache_dir,
        )
        records = adapter.parse(raw, fetched, ticker=args.ticker)
    ids = ingest_records(records, EvidenceStore(database), dry_run=args.dry_run)
    print(json.dumps({"ok": True, "dry_run": args.dry_run, "records": len(ids)}))
    return 0


def _cmd_screen(args: argparse.Namespace) -> int:
    from tradehub_research.screening import load_config, run_screening

    database = _database(args)
    if not args.as_of or not args.config:
        raise SystemExit("screen requires --as-of RFC3339 and --config PATH")
    config = load_config(args.config)
    run_id = run_screening(args.as_of, args.snapshot, config, database=database)
    print(json.dumps({"ok": True, "run_id": run_id}))
    return 0


def _load_snapshot_file(path: Path) -> dict:
    from tradehub_research.portfolio.snapshot import build_snapshot

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("snapshot file must be a JSON object")
    return build_snapshot(
        raw["as_of"],
        currency=raw.get("currency", "USD"),
        cash_microusd=raw.get("cash_microusd"),
        cash_status=raw.get("cash_status", "KNOWN"),
        nav_microusd=raw.get("nav_microusd"),
        valuation_status=raw.get("valuation_status", "KNOWN"),
        holdings_status=raw.get("holdings_status", "KNOWN"),
        provenance=raw.get("provenance", {"kind": "cli"}),
        holdings=raw.get("holdings", []),
        market_inputs=raw.get("market_inputs", []),
    )


def _load_signals_file(path: Path | None, as_of: str) -> list:
    from tradehub_research.portfolio.snapshot import build_signal_input

    if path is None:
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("signals"), list):
        raise SystemExit("signals file must be a JSON object with a 'signals' list")
    signals = []
    for item in raw["signals"]:
        signals.append(
            build_signal_input(
                item["security_id"],
                item.get("as_of", as_of),
                remaining_opportunity_ppm=item.get("remaining_opportunity_ppm"),
                opportunity_status=item.get("opportunity_status", "KNOWN"),
                source_kind=item.get("source_kind", "FIXTURE"),
                evidence_ids=item.get("evidence_ids", []),
            )
        )
    return signals


def _cmd_portfolio_policy_register(args: argparse.Namespace) -> int:
    from tradehub_research.portfolio.policy import PolicyRegistry, load_policy_from_json
    from tradehub_research.portfolio.types import PolicyStatus

    database = _database(args)
    raw = args.file.read_text(encoding="utf-8")
    status = PolicyStatus(args.status)
    policy = load_policy_from_json(args.version, status, raw)
    if status == PolicyStatus.PAPER:
        if not args.approved_by or not args.approved_at:
            raise SystemExit("PAPER policy registration requires --approved-by and --approved-at")
        import json as _json

        from tradehub_research.portfolio.policy import build_policy

        policy = build_policy(
            args.version,
            status,
            _json.loads(raw),
            approved_by=args.approved_by,
            approved_at=args.approved_at,
        )
    PolicyRegistry(database).register(policy)
    print(
        json.dumps(
            {
                "ok": True,
                "policy_version": policy.policy_version,
                "policy_status": policy.policy_status.value,
                "sizing_policy_version": policy.sizing_policy_version,
                "spec_hash": policy.spec_hash,
            }
        )
    )
    return 0


def _cmd_portfolio_run(args: argparse.Namespace) -> int:
    from tradehub_research.portfolio.engine import PortfolioEngine

    database = _database(args)
    if not args.pipeline_run or not args.policy or not args.snapshot or not args.as_of:
        raise SystemExit("portfolio run requires --pipeline-run, --policy, --snapshot, and --as-of")
    snapshot = _load_snapshot_file(args.snapshot)
    signals = _load_signals_file(args.signals, args.as_of)
    summary = PortfolioEngine(database).run(
        pipeline_run_id=args.pipeline_run,
        policy_version=args.policy,
        snapshot=snapshot,
        decision_as_of=args.as_of,
        signals=signals,
        allow_provisional=args.allow_provisional,
        allow_fixture=args.allow_fixture,
    )
    print(json.dumps(summary.as_dict(), indent=2))
    return 0


def _cmd_portfolio_replay(args: argparse.Namespace) -> int:
    from tradehub_research.portfolio.types import C, D

    database = _database(args)
    with database.connect(read_only=True) as db:
        row = db.execute("SELECT * FROM portfolio_run WHERE run_id=?", (args.run_id,)).fetchone()
        if row is None:
            raise SystemExit(f"no portfolio run {args.run_id!r}")
        observation_count = db.execute(
            "SELECT count(*) FROM portfolio_state_observation WHERE run_id=?",
            (args.run_id,),
        ).fetchone()[0]
        transition_count = db.execute(
            "SELECT count(*) FROM portfolio_state_transition WHERE decision_id IN "
            "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
            (args.run_id,),
        ).fetchone()[0]
        proposal_count = db.execute(
            "SELECT count(*) FROM trade_proposal WHERE decision_id IN "
            "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
            (args.run_id,),
        ).fetchone()[0]
    # The stored input_hash includes the pinned policy spec hash; recompute it
    # exactly as the engine does from the policy registry.
    from tradehub_research.portfolio.policy import PolicyRegistry

    try:
        policy = PolicyRegistry(database).get(row["policy_version"])
        recomputed_input_hash = C(
            {
                "invocation_key": row["invocation_key"],
                "state_prestate_hash": row["state_prestate_hash"],
                "market_data_prestate_hash": row["market_data_prestate_hash"],
                "budget_prestate_hash": row["budget_prestate_hash"],
                "policy_spec_hash": policy.spec_hash,
                "score_set_hash": row["score_set_hash"],
                "signal_set_hash": row["signal_set_hash"],
                "candidate_set_hash": row["candidate_set_hash"],
            }
        )
        replayable = recomputed_input_hash == row["input_hash"]
    except KeyError:
        replayable = False
    consistent = (
        replayable
        and observation_count == row["expected_security_count"]
        and row["run_id"] == D("portfolio-run-v1", recomputed_input_hash)
    )
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": row["run_id"],
                "replayable": consistent,
                "observation_count": observation_count,
                "transition_count": transition_count,
                "proposal_count": proposal_count,
                "expected_security_count": row["expected_security_count"],
            },
            indent=2,
        )
    )
    return 0 if consistent else 1


def _cmd_portfolio_briefing(args: argparse.Namespace) -> int:
    database = _database(args)
    with database.connect(read_only=True) as db:
        if args.run_id:
            row = db.execute(
                "SELECT body_text FROM portfolio_briefing WHERE run_id=?",
                (args.run_id,),
            ).fetchone()
        elif args.latest:
            row = db.execute(
                "SELECT body_text FROM portfolio_briefing ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        else:
            raise SystemExit("briefing requires --run-id or --latest")
        if row is None:
            raise SystemExit("no briefing found")
    print(row["body_text"], end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-db")
    parser.add_argument("--db", type=Path, help="research.db path (default: RESEARCH_DB_PATH)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create/migrate the research database")
    init_parser.set_defaults(handler=_cmd_init)

    check_parser = subparsers.add_parser("check", help="integrity check")
    check_parser.set_defaults(handler=_cmd_check)

    ingest_parser = subparsers.add_parser("ingest", help="ingest a raw provider fixture")
    ingest_parser.add_argument("--provider", choices=("sec", "tiingo"))
    ingest_parser.add_argument("--fixture", type=Path)
    ingest_parser.add_argument("--kind", choices=("index", "companyfacts", "form4", "eod"))
    ingest_parser.add_argument("--ticker")
    ingest_parser.add_argument("--security-id")
    ingest_parser.add_argument("--accession")
    ingest_parser.add_argument("--filed")
    ingest_parser.add_argument("--acceptance-time")
    ingest_parser.add_argument("--supersedes-accession")
    ingest_parser.add_argument("--dry-run", action="store_true")
    ingest_parser.set_defaults(handler=_cmd_ingest)

    screen_parser = subparsers.add_parser("screen", help="run the deterministic screening pipeline")
    screen_parser.add_argument("--as-of", help="RFC3339 as-of timestamp")
    screen_parser.add_argument("--snapshot", default=None)
    screen_parser.add_argument("--config", type=Path)
    screen_parser.set_defaults(handler=_cmd_screen)

    portfolio = subparsers.add_parser("portfolio", help="portfolio plane commands")
    portfolio_sub = portfolio.add_subparsers(dest="portfolio_command", required=True)

    policy_register = portfolio_sub.add_parser(
        "policy-register", help="register a versioned portfolio policy"
    )
    policy_register.add_argument("--file", type=Path, required=True)
    policy_register.add_argument("--version", required=True)
    policy_register.add_argument(
        "--status", choices=("FIXTURE", "PROVISIONAL", "PAPER"), required=True
    )
    policy_register.add_argument("--approved-by")
    policy_register.add_argument("--approved-at")
    policy_register.set_defaults(handler=_cmd_portfolio_policy_register)

    portfolio_run = portfolio_sub.add_parser("run", help="execute one deterministic decision run")
    portfolio_run.add_argument("--pipeline-run", required=True)
    portfolio_run.add_argument("--policy", required=True)
    portfolio_run.add_argument("--snapshot", type=Path, required=True)
    portfolio_run.add_argument("--signals", type=Path)
    portfolio_run.add_argument("--as-of", required=True)
    portfolio_run.add_argument("--allow-provisional", action="store_true")
    portfolio_run.add_argument("--allow-fixture", action="store_true")
    portfolio_run.set_defaults(handler=_cmd_portfolio_run)

    replay_parser = portfolio_sub.add_parser("replay", help="verify a stored run's reproducibility")
    replay_parser.add_argument("--run-id", required=True)
    replay_parser.set_defaults(handler=_cmd_portfolio_replay)

    briefing_parser = portfolio_sub.add_parser("briefing", help="render a stored briefing")
    briefing_parser.add_argument("--run-id")
    briefing_parser.add_argument("--latest", action="store_true")
    briefing_parser.set_defaults(handler=_cmd_portfolio_briefing)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

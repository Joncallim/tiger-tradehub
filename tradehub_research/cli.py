from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-db")
    parser.add_argument("command", choices=("init", "migrate", "check", "screen", "ingest"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", help="RFC3339 as-of timestamp (screen)")
    parser.add_argument("--snapshot", default=None, help="snapshot_version id (screen)")
    parser.add_argument("--config", type=Path, help="screening config JSON (screen)")
    parser.add_argument("--dry-run", action="store_true", help="validate ingestion without writes")
    parser.add_argument("--provider", choices=("sec", "tiingo"), help="provider (ingest)")
    parser.add_argument("--fixture", type=Path, help="raw fixture to parse (ingest)")
    parser.add_argument(
        "--kind", choices=("index", "companyfacts", "form4", "eod"), help="fixture kind"
    )
    parser.add_argument("--ticker", help="canonical ticker for Tiingo fixture")
    parser.add_argument("--security-id", help="canonical security identity for SEC fixture")
    parser.add_argument("--accession", help="SEC Form 4 accession")
    parser.add_argument("--filed", help="SEC filed date (required for Form 4 fallback PAT)")
    parser.add_argument("--acceptance-time", help="SEC source-reported acceptanceDateTime")
    parser.add_argument("--supersedes-accession", help="original accession replaced by Form 4/A")
    args = parser.parse_args(argv)

    settings = ResearchSettings()
    database = ResearchDB(args.db or settings.db_path, settings.busy_timeout_ms)

    if args.command in {"init", "migrate"}:
        version = database.migrate()
        print(json.dumps({"ok": True, "schema_version": version, "path": str(database.path)}))
        return 0
    if args.command == "check":
        result = database.check()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1

    if args.command == "ingest":
        if not args.provider or not args.fixture or not args.kind:
            parser.error("ingest requires --provider, --kind, and --fixture")
        from tradehub_research.adapters.base import FetchResult, ingest_records
        from tradehub_research.adapters.sec import SecAdapter
        from tradehub_research.adapters.tiingo import TiingoEodAdapter
        from tradehub_research.evidence import EvidenceStore

        raw = args.fixture.read_bytes()
        fetched = FetchResult(
            args.fixture.resolve().as_uri(), "2099-01-01T00:00:00Z", 200, {}, raw, args.fixture
        )
        if args.provider == "sec":
            if not settings.sec_user_agent:
                parser.error("SEC ingestion requires RESEARCH_SEC_USER_AGENT")
            adapter = SecAdapter(
                user_agent=settings.sec_user_agent, cache_dir=settings.adapter_cache_dir
            )
            if args.kind == "index":
                records = adapter.parse_daily_index(raw, fetched)
                if args.security_id:
                    index_dates = sorted({r.structured_fields["filed"] for r in records})
                    if not index_dates and args.filed:
                        index_dates = [args.filed]  # settled-empty/non-publication marker
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
                parser.error(
                    "SEC kind must be index/companyfacts or form4 with --accession/--filed"
                )
            if not args.security_id:
                parser.error("SEC fixture ingestion requires canonical --security-id")
            records = adapter.with_security(records, args.security_id)
        else:
            if args.kind != "eod" or not args.ticker:
                parser.error("Tiingo ingestion requires --kind eod and --ticker")
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

    # screen
    if not args.as_of or not args.config:
        parser.error("screen requires --as-of RFC3339 and --config PATH")
    from tradehub_research.screening import load_config, run_screening

    config = load_config(args.config)
    run_id = run_screening(args.as_of, args.snapshot, config, database=database)
    print(json.dumps({"ok": True, "run_id": run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

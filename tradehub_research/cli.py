from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-db")
    parser.add_argument("command", choices=("init", "migrate", "check", "screen"))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", help="RFC3339 as-of timestamp (screen)")
    parser.add_argument("--snapshot", default=None, help="snapshot_version id (screen)")
    parser.add_argument("--config", type=Path, help="screening config JSON (screen)")
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

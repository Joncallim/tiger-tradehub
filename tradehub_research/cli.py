from __future__ import annotations

import argparse
import json
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-db")
    parser.add_argument("command", choices=("init", "migrate", "check"))
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    settings = ResearchSettings()
    database = ResearchDB(args.db or settings.db_path, settings.busy_timeout_ms)
    if args.command in {"init", "migrate"}:
        version = database.migrate()
        print(json.dumps({"ok": True, "schema_version": version, "path": str(database.path)}))
        return 0
    result = database.check()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

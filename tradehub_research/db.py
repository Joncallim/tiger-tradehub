from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tradehub_research.schema import MIGRATIONS, PHASE_0_SCHEMA_VERSION


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchDB:
    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connect(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        if read_only:
            connection = sqlite3.connect(
                f"file:{self.path.resolve()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            if not read_only:
                connection.commit()
        except Exception:
            if not read_only:
                connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> int:
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version_id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
                "description TEXT NOT NULL)"
            )
            db.commit()
            applied = {row[0] for row in db.execute("SELECT version_id FROM schema_version")}
            for version, description, sql in MIGRATIONS:
                if version not in applied:
                    values = (description.replace("'", "''"), utc_now().replace("'", "''"))
                    db.executescript(
                        f"BEGIN IMMEDIATE;\n{sql}\n"
                        f"INSERT INTO schema_version VALUES ({version}, '{values[1]}', "
                        f"'{values[0]}');\nCOMMIT;"
                    )
        return self.schema_version()

    init = migrate

    def schema_version(self) -> int:
        with self.connect(read_only=True) as db:
            row = db.execute("SELECT MAX(version_id) FROM schema_version").fetchone()
        return int(row[0] or 0)

    def check(self) -> dict[str, object]:
        with self.connect(read_only=True) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {
            "ok": integrity == "ok" and self.schema_version() == PHASE_0_SCHEMA_VERSION,
            "integrity": integrity,
            "schema_version": self.schema_version(),
            "tables": sorted(tables),
        }

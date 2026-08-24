from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

from tradehub_research.db import ResearchDB, utc_now


def create_snapshot(
    database: ResearchDB, dest_path: Path | str, scope: str = "phase-0 full DB"
) -> str:
    destination = Path(dest_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot_id = str(uuid.uuid4())
    with database.connect(read_only=True) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    with database.connect() as db:
        db.execute(
            "INSERT INTO snapshot_version VALUES (?,?,?,?,?)",
            (snapshot_id, database.schema_version(), scope, utc_now(), digest),
        )
    return snapshot_id


def open_snapshot_read_only(path: Path | str, timeout_seconds: float = 5.0) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=timeout_seconds
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
    return connection

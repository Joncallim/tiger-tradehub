from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradehub_research.db import ResearchDB, utc_now


def _content_hash(connection: sqlite3.Connection) -> str:
    """Hash all snapshot schema/data except the self-referential manifest."""
    digest = hashlib.sha256()
    tables = connection.execute(
        """SELECT name,sql FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'snapshot_manifest'
        ORDER BY name"""
    ).fetchall()
    for table_name, schema_sql in tables:
        digest.update(json.dumps([table_name, schema_sql], separators=(",", ":")).encode())
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')]
        ordering = ",".join(f'"{column}"' for column in columns)
        for row in connection.execute(f'SELECT * FROM "{table_name}" ORDER BY {ordering}'):
            digest.update(json.dumps(list(row), separators=(",", ":"), default=str).encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class SnapshotHandle:
    path: Path
    manifest: dict[str, Any]
    timeout_seconds: float = 5.0

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.resolve()}?mode=ro", uri=True, timeout=self.timeout_seconds
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        connection.execute("PRAGMA query_only=ON")
        return connection


def create_snapshot(
    database: ResearchDB, dest_path: Path | str, scope: str = "phase-0 full DB"
) -> str:
    destination = Path(dest_path).resolve()
    live_path = database.path.resolve()
    if destination == live_path:
        raise ValueError("snapshot destination cannot be the live database")
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    snapshot_id = str(uuid.uuid4())
    created_at = utc_now()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with database.connect(read_only=True) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
        with sqlite3.connect(temporary) as copied:
            copied.execute("PRAGMA journal_mode=DELETE")
            if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("snapshot copy failed integrity check")
        with sqlite3.connect(temporary) as copied:
            digest = _content_hash(copied)
            copied.execute(
                "INSERT INTO snapshot_manifest VALUES (?,?,?,?)",
                (snapshot_id, created_at, str(live_path), digest),
            )
            copied.commit()
        # Registration rolls back if atomic publication fails. A hard crash may leave
        # an unregistered temp sibling; recovery is delete-the-temp and rerun.
        with database.connect() as db:
            db.execute(
                "INSERT INTO snapshot_version VALUES (?,?,?,?,?)",
                (snapshot_id, database.schema_version(), scope, created_at, digest),
            )
            os.replace(temporary, destination)
        return snapshot_id
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def open_snapshot_read_only(path: Path | str, timeout_seconds: float = 5.0) -> SnapshotHandle:
    snapshot_path = Path(path).resolve()
    connection = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("snapshot failed integrity check")
        rows = connection.execute("SELECT * FROM snapshot_manifest").fetchall()
        if len(rows) != 1:
            raise sqlite3.DatabaseError("snapshot manifest is missing or ambiguous")
        manifest = dict(rows[0])
        if _content_hash(connection) != manifest["content_hash"]:
            raise sqlite3.DatabaseError("snapshot content hash does not match manifest")
    finally:
        connection.close()
    return SnapshotHandle(snapshot_path, manifest, timeout_seconds)

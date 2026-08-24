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


def _frame(digest: Any, value: Any) -> None:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _content_hash(connection: sqlite3.Connection) -> str:
    """Hash length-framed schema objects and rows, excluding manifest row content."""
    digest = hashlib.sha256()
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    for object_type, object_name, schema_sql in objects:
        _frame(digest, [object_type, object_name, schema_sql])
    tables = connection.execute(
        """SELECT name FROM sqlite_master WHERE type='table'
        AND name != 'snapshot_manifest' ORDER BY name"""
    ).fetchall()
    for (table_name,) in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')]
        ordering = ",".join(f'"{column}"' for column in columns)
        for row in connection.execute(f'SELECT * FROM "{table_name}" ORDER BY {ordering}'):
            _frame(digest, [table_name, list(row)])
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
        try:
            connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute("SELECT * FROM snapshot_manifest").fetchall()
            if len(rows) != 1:
                raise sqlite3.DatabaseError("snapshot manifest is missing or ambiguous")
            manifest = dict(rows[0])
            if manifest != self.manifest:
                raise sqlite3.DatabaseError("snapshot identity does not match verified handle")
            if _content_hash(connection) != self.manifest["content_hash"]:
                raise sqlite3.DatabaseError("snapshot content hash does not match manifest")
            if not _registration_is_ready(manifest):
                raise sqlite3.DatabaseError("snapshot registration is not READY")
            return connection
        except Exception:
            connection.close()
            raise


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
    _recover_pending(database)
    with database.connect() as db:
        db.execute(
            """INSERT INTO snapshot_version(
                snapshot_id,created_from_db_version,scope_description,created_at,content_hash,
                status,destination_path) VALUES (?,?,?,?,?,'PENDING',?)""",
            (
                snapshot_id,
                database.schema_version(),
                scope,
                created_at,
                "PENDING",
                str(destination),
            ),
        )
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
        with temporary.open("rb") as snapshot_file:
            os.fsync(snapshot_file.fileno())
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with database.connect() as db:
            db.execute(
                "UPDATE snapshot_version SET content_hash=?,status='READY' WHERE snapshot_id=?",
                (digest, snapshot_id),
            )
        return snapshot_id
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _recover_pending(database: ResearchDB) -> None:
    with database.connect() as db:
        pending = db.execute(
            "SELECT snapshot_id,destination_path FROM snapshot_version WHERE status='PENDING'"
        ).fetchall()
        for row in pending:
            path = Path(row["destination_path"]) if row["destination_path"] else None
            if path is None or not path.exists():
                db.execute(
                    "DELETE FROM snapshot_version WHERE snapshot_id=?", (row["snapshot_id"],)
                )
                continue
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
                connection.row_factory = sqlite3.Row
                manifest_row = connection.execute("SELECT * FROM snapshot_manifest").fetchone()
                valid = (
                    manifest_row is not None
                    and manifest_row["snapshot_id"] == row["snapshot_id"]
                    and _content_hash(connection) == manifest_row["content_hash"]
                )
            except sqlite3.Error:
                valid = False
            finally:
                if connection is not None:
                    connection.close()
            if valid:
                db.execute(
                    "UPDATE snapshot_version SET content_hash=?,status='READY' WHERE snapshot_id=?",
                    (manifest_row["content_hash"], row["snapshot_id"]),
                )


def _registration_is_ready(manifest: dict[str, Any]) -> bool:
    connection: sqlite3.Connection | None = None
    try:
        source = Path(manifest["source_db"])
        connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT status FROM snapshot_version WHERE snapshot_id=?",
            (manifest["snapshot_id"],),
        ).fetchone()
        return row is not None and row[0] == "READY"
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


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
        if not _registration_is_ready(manifest):
            raise sqlite3.DatabaseError("snapshot registration is not READY")
    finally:
        connection.close()
    return SnapshotHandle(snapshot_path, manifest, timeout_seconds)

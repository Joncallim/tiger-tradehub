from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tradehub.models import OrderIntent


class AuditStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmations (
                    token TEXT PRIMARY KEY,
                    intent_json TEXT NOT NULL,
                    tiger_preview_json TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    claimed_at TEXT,
                    submitted_at TEXT,
                    order_id TEXT
                )
                """
            )
            self._add_column_if_missing(db, "confirmations", "claimed_at", "TEXT")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )

    def _add_column_if_missing(
        self, db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_events(created_at, event_type, payload_json) VALUES (?, ?, ?)",
                (utc_now().isoformat(), event_type, json.dumps(payload, default=str)),
            )

    def create_confirmation(
        self,
        intent: OrderIntent,
        tiger_preview: dict[str, Any] | None,
        ttl_seconds: int,
    ) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(24)
        expires_at = utc_now() + timedelta(seconds=ttl_seconds)
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO confirmations(
                    token, intent_json, tiger_preview_json, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token,
                    intent.model_dump_json(),
                    json.dumps(tiger_preview, default=str) if tiger_preview else None,
                    utc_now().isoformat(),
                    expires_at.isoformat(),
                ),
            )
        self.record_event(
            "preview_created",
            {"symbol": intent.symbol, "side": intent.side, "quantity": intent.quantity},
        )
        return token, expires_at

    def consume_confirmation(self, token: str) -> tuple[OrderIntent, dict[str, Any] | None]:
        return self.claim_confirmation(token)

    def claim_confirmation(self, token: str) -> tuple[OrderIntent, dict[str, Any] | None]:
        now = utc_now().isoformat()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE confirmations
                SET claimed_at = ?
                WHERE token = ?
                  AND submitted_at IS NULL
                  AND claimed_at IS NULL
                  AND expires_at >= ?
                """,
                (now, token, now),
            )
            row = db.execute(
                "SELECT * FROM confirmations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown confirmation token")
            if cursor.rowcount == 1:
                preview = (
                    json.loads(row["tiger_preview_json"]) if row["tiger_preview_json"] else None
                )
                return OrderIntent.model_validate_json(row["intent_json"]), preview
            if row["submitted_at"]:
                raise ValueError("confirmation token has already been submitted")
            if row["claimed_at"]:
                raise ValueError("confirmation token is already being submitted")
            if datetime.fromisoformat(row["expires_at"]) < utc_now():
                raise ValueError("confirmation token has expired")
            raise ValueError("confirmation token could not be claimed")

    def finalize_confirmation(self, token: str, order_id: str | None = None) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE confirmations
                SET submitted_at = ?, claimed_at = NULL, order_id = ?
                WHERE token = ? AND claimed_at IS NOT NULL AND submitted_at IS NULL
                """,
                (utc_now().isoformat(), order_id, token),
            )
            if cursor.rowcount != 1:
                raise ValueError("confirmation token is not claimed")

    def release_confirmation(self, token: str) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE confirmations
                SET claimed_at = NULL
                WHERE token = ? AND submitted_at IS NULL
                """,
                (token,),
            )

    def mark_order_id(self, token: str, order_id: str | None) -> None:
        with self.connect() as db:
            db.execute("UPDATE confirmations SET order_id = ? WHERE token = ?", (order_id, token))


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)

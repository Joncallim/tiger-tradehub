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

STALE_CLAIM_SECONDS = 120
CONFIRMATION_STATE_READY = "READY"
CONFIRMATION_STATE_INDETERMINATE = "INDETERMINATE"
CONFIRMATION_STATE_SUBMITTED = "SUBMITTED"


def _coalesce_state(value: str | None) -> str:
    return value or CONFIRMATION_STATE_READY


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
                    order_id TEXT,
                    submission_state TEXT,
                    reserved_order_id TEXT
                )
                """
            )
            self._add_column_if_missing(db, "confirmations", "claimed_at", "TEXT")
            self._add_column_if_missing(db, "confirmations", "submission_state", "TEXT")
            self._add_column_if_missing(db, "confirmations", "reserved_order_id", "TEXT")
            db.execute(
                """
                UPDATE confirmations
                SET submission_state = ?
                WHERE submitted_at IS NULL AND submission_state IS NULL
                """,
                (CONFIRMATION_STATE_READY,),
            )
            db.execute(
                """
                UPDATE confirmations
                SET submission_state = ?
                WHERE submitted_at IS NOT NULL AND submission_state IS NULL
                """,
                (CONFIRMATION_STATE_SUBMITTED,),
            )
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
                    token, intent_json, tiger_preview_json, created_at, expires_at, submission_state
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    intent.model_dump_json(),
                    json.dumps(tiger_preview, default=str) if tiger_preview else None,
                    utc_now().isoformat(),
                    expires_at.isoformat(),
                    CONFIRMATION_STATE_READY,
                ),
            )
        self.record_event(
            "preview_created",
            {"symbol": intent.symbol, "side": intent.side, "quantity": intent.quantity},
        )
        return token, expires_at

    def consume_confirmation(self, token: str) -> tuple[OrderIntent, dict[str, Any] | None]:
        return self.claim_confirmation(token)

    def get_confirmation(self, token: str) -> tuple[OrderIntent, dict[str, Any] | None, str | None]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM confirmations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown confirmation token")
            preview = json.loads(row["tiger_preview_json"]) if row["tiger_preview_json"] else None
            return (
                OrderIntent.model_validate_json(row["intent_json"]),
                preview,
                row["reserved_order_id"],
            )

    def claim_confirmation(self, token: str) -> tuple[OrderIntent, dict[str, Any] | None]:
        now = utc_now()
        stale_before = now - timedelta(seconds=STALE_CLAIM_SECONDS)
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE confirmations
                SET claimed_at = ?
                WHERE token = ?
                  AND submitted_at IS NULL
                  AND COALESCE(submission_state, ?) = ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                  AND expires_at >= ?
                """,
                (
                    now.isoformat(),
                    token,
                    CONFIRMATION_STATE_READY,
                    CONFIRMATION_STATE_READY,
                    stale_before.isoformat(),
                    now.isoformat(),
                ),
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
            if _coalesce_state(row["submission_state"]) == CONFIRMATION_STATE_INDETERMINATE:
                raise ValueError(
                    "confirmation token is in indeterminate state and must be reconciled"
                )
            if row["claimed_at"]:
                raise ValueError("confirmation token is already being submitted")
            if datetime.fromisoformat(row["expires_at"]) < now:
                raise ValueError("confirmation token has expired")
            raise ValueError("confirmation token could not be claimed")

    def finalize_confirmation(self, token: str, order_id: str | None = None) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE confirmations
                SET
                    submitted_at = ?,
                    claimed_at = NULL,
                    submission_state = ?,
                    order_id = ?
                WHERE token = ? AND submitted_at IS NULL AND claimed_at IS NOT NULL
                """,
                (
                    utc_now().isoformat(),
                    CONFIRMATION_STATE_SUBMITTED,
                    order_id,
                    token,
                ),
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
                  AND COALESCE(submission_state, ?) = ?
                """,
                (token, CONFIRMATION_STATE_READY, CONFIRMATION_STATE_READY),
            )

    def mark_order_id(self, token: str, order_id: str | None) -> None:
        with self.connect() as db:
            db.execute("UPDATE confirmations SET order_id = ? WHERE token = ?", (order_id, token))

    def record_reserved_order_id(self, token: str, order_id: str | None) -> None:
        if order_id is None:
            return
        with self.connect() as db:
            db.execute(
                """
                UPDATE confirmations
                SET reserved_order_id = COALESCE(reserved_order_id, ?)
                WHERE token = ? AND submitted_at IS NULL
                """,
                (order_id, token),
            )

    def mark_submission_indeterminate(
        self, token: str, reserved_order_id: str | None = None
    ) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE confirmations
                SET
                    submission_state = ?,
                    reserved_order_id = COALESCE(reserved_order_id, ?),
                    claimed_at = COALESCE(claimed_at, ?)
                WHERE token = ? AND submitted_at IS NULL
                """,
                (
                    CONFIRMATION_STATE_INDETERMINATE,
                    reserved_order_id,
                    utc_now().isoformat(),
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("confirmation token is not claimed")

    def load_indeterminate_confirmation(
        self, token: str
    ) -> tuple[OrderIntent, dict[str, Any] | None, str | None]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM confirmations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown confirmation token")
            if row["submitted_at"]:
                raise ValueError("confirmation token has already been submitted")
            if _coalesce_state(row["submission_state"]) != CONFIRMATION_STATE_INDETERMINATE:
                raise ValueError("confirmation token is not in reconciliation-required state")
            preview = json.loads(row["tiger_preview_json"]) if row["tiger_preview_json"] else None
            return (
                OrderIntent.model_validate_json(row["intent_json"]),
                preview,
                row["reserved_order_id"],
            )

    def mark_reconciliation_retry_allowed(self, token: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE confirmations
                SET claimed_at = NULL, submission_state = ?
                WHERE token = ? AND submitted_at IS NULL AND COALESCE(submission_state, ?) = ?
                """,
                (
                    CONFIRMATION_STATE_READY,
                    token,
                    CONFIRMATION_STATE_INDETERMINATE,
                    CONFIRMATION_STATE_INDETERMINATE,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("confirmation token is not in reconciliation-required state")

    def get_submission_state(self, token: str) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT submission_state FROM confirmations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown confirmation token")
            return row["submission_state"]


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)

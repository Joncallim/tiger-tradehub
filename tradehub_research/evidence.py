from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from tradehub_research.db import ResearchDB, utc_now


class EvidenceStore:
    def __init__(self, database: ResearchDB):
        self.database = database

    def insert(
        self,
        *,
        security_id: str,
        source_id: str,
        structured_fields: dict[str, Any],
        extraction_confidence: float,
        event_time: str,
        public_available_time: str | None,
        pat_provenance: str,
        ingested_time: str | None = None,
        content_hash: str | None = None,
        supersedes_evidence_id: str | None = None,
        withdrawn: bool = False,
        evidence_id: str | None = None,
    ) -> str:
        content = json.dumps(structured_fields, sort_keys=True, separators=(",", ":"))
        digest = content_hash or hashlib.sha256(content.encode()).hexdigest()
        identifier = evidence_id or str(uuid.uuid4())
        try:
            with self.database.connect() as db:
                db.execute(
                    """INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identifier,
                        security_id,
                        source_id,
                        content,
                        extraction_confidence,
                        supersedes_evidence_id,
                        int(withdrawn),
                        digest,
                        event_time,
                        public_available_time,
                        pat_provenance,
                        ingested_time or utc_now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            with self.database.connect(read_only=True) as db:
                existing = db.execute(
                    "SELECT evidence_id FROM evidence_event "
                    "WHERE source_id=? AND security_id=? AND content_hash=?",
                    (source_id, security_id, digest),
                ).fetchone()
            if existing is None:
                raise exc
            return str(existing[0])
        return identifier

    def historical(self, as_of: str, security_id: str | None = None) -> list[sqlite3.Row]:
        security_clause = "AND e.security_id = ?" if security_id else ""
        parameters: list[Any] = [as_of]
        if security_id:
            parameters.append(security_id)
        parameters.append(as_of)
        sql = f"""
            SELECT e.* FROM evidence_event e
            WHERE e.public_available_time IS NOT NULL
              AND e.pat_provenance IN ('source_reported','derived_from_index')
              AND e.public_available_time <= ? {security_clause}
              AND e.withdrawn = 0
              AND NOT EXISTS (
                SELECT 1 FROM evidence_event successor
                WHERE successor.supersedes_evidence_id = e.evidence_id
                  AND successor.public_available_time IS NOT NULL
                  AND successor.pat_provenance IN ('source_reported','derived_from_index')
                  AND successor.public_available_time <= ?
              )
            ORDER BY e.public_available_time, e.evidence_id
        """
        with self.database.connect(read_only=True) as db:
            return list(db.execute(sql, parameters))

    def current(self, security_id: str | None = None) -> list[sqlite3.Row]:
        clause = "AND e.security_id = ?" if security_id else ""
        with self.database.connect(read_only=True) as db:
            return list(
                db.execute(
                    f"""SELECT e.* FROM evidence_event e WHERE e.withdrawn = 0 {clause}
                    AND NOT EXISTS (SELECT 1 FROM evidence_event successor
                        WHERE successor.supersedes_evidence_id=e.evidence_id)
                    ORDER BY e.ingested_time, e.evidence_id""",
                    (security_id,) if security_id else (),
                )
            )

    def provenance_histogram(self) -> list[sqlite3.Row]:
        with self.database.connect(read_only=True) as db:
            return list(
                db.execute(
                    "SELECT source_id, pat_provenance, COUNT(*) AS count FROM evidence_event "
                    "GROUP BY source_id, pat_provenance ORDER BY source_id, pat_provenance"
                )
            )

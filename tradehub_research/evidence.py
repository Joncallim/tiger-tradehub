from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts, utc_now


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
        source_record_id: str | None = None,
        supersedes_evidence_id: str | None = None,
        withdrawn: bool = False,
        evidence_id: str | None = None,
    ) -> str:
        content = json.dumps(structured_fields, sort_keys=True, separators=(",", ":"))
        digest = content_hash or hashlib.sha256(content.encode()).hexdigest()
        identifier = evidence_id or str(uuid.uuid4())
        event_time = normalize_ts(event_time)
        public_available_time = (
            normalize_ts(public_available_time) if public_available_time is not None else None
        )
        ingested_time = normalize_ts(ingested_time or utc_now())
        if public_available_time is not None and public_available_time > ingested_time:
            raise ValueError("public_available_time cannot follow ingested_time")

        values = (
            identifier,
            security_id,
            source_id,
            content,
            extraction_confidence,
            supersedes_evidence_id,
            int(withdrawn),
            digest,
            source_record_id,
            event_time,
            public_available_time,
            pat_provenance,
            ingested_time,
        )
        with self.database.connect() as db:
            if supersedes_evidence_id is not None:
                if supersedes_evidence_id == identifier:
                    raise ValueError("evidence cannot supersede itself")
                predecessor = db.execute(
                    "SELECT * FROM evidence_event WHERE evidence_id=?",
                    (supersedes_evidence_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("superseded evidence does not exist")
                if (
                    predecessor["security_id"] != security_id
                    or predecessor["source_id"] != source_id
                ):
                    raise ValueError("supersession requires the same security and source")
                predecessor_pat = predecessor["public_available_time"]
                if predecessor_pat is not None and (
                    public_available_time is None or public_available_time < predecessor_pat
                ):
                    raise ValueError("supersession cannot backdate public availability")
            if source_record_id is None:
                conflict = (
                    "(source_id,security_id,event_time,content_hash) WHERE source_record_id IS NULL"
                )
                lookup = (
                    "source_id=? AND security_id=? AND event_time=? AND content_hash=? "
                    "AND source_record_id IS NULL",
                    (source_id, security_id, event_time, digest),
                )
            else:
                conflict = (
                    "(source_id,security_id,source_record_id) WHERE source_record_id IS NOT NULL"
                )
                lookup = (
                    "source_id=? AND security_id=? AND source_record_id=?",
                    (source_id, security_id, source_record_id),
                )
            cursor = db.execute(
                f"""INSERT INTO evidence_event(
                    evidence_id,security_id,source_id,structured_fields,extraction_confidence,
                    supersedes_evidence_id,withdrawn,content_hash,source_record_id,event_time,
                    public_available_time,pat_provenance,ingested_time
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT {conflict} DO NOTHING""",
                values,
            )
            if cursor.rowcount == 0:
                existing = db.execute(
                    f"SELECT * FROM evidence_event WHERE {lookup[0]}", lookup[1]
                ).fetchone()
                assert existing is not None
                comparable = (
                    "structured_fields",
                    "extraction_confidence",
                    "supersedes_evidence_id",
                    "withdrawn",
                    "content_hash",
                    "event_time",
                    "public_available_time",
                    "pat_provenance",
                )
                submitted = dict(
                    zip(
                        (
                            "evidence_id",
                            "security_id",
                            "source_id",
                            "structured_fields",
                            "extraction_confidence",
                            "supersedes_evidence_id",
                            "withdrawn",
                            "content_hash",
                            "source_record_id",
                            "event_time",
                            "public_available_time",
                            "pat_provenance",
                            "ingested_time",
                        ),
                        values,
                        strict=True,
                    )
                )
                if any(existing[key] != submitted[key] for key in comparable):
                    raise ValueError("identity retry metadata does not match existing evidence")
                return str(existing["evidence_id"])
        return identifier

    def historical(self, as_of: str, security_id: str | None = None) -> list[sqlite3.Row]:
        security_clause = "AND e.security_id = ?" if security_id else ""
        as_of = normalize_ts(as_of)
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

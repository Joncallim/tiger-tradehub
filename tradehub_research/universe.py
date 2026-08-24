from __future__ import annotations

import sqlite3
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts


class UniverseMembershipStore:
    """Append-only effective-dated facts, gated by when each fact became knowable."""

    def __init__(self, database: ResearchDB):
        self.database = database

    def insert(
        self,
        *,
        security_id: str,
        valid_from: str,
        knowledge_time: str,
        pat_provenance: str,
        valid_to: str | None = None,
        supersedes_id: int | None = None,
        price: float | None = None,
        market_cap: float | None = None,
        avg_dollar_volume: float | None = None,
        price_eligible: bool,
        market_cap_eligible: bool,
        liquidity_eligible: bool,
        eligible: bool,
    ) -> int:
        valid_from = normalize_ts(valid_from)
        valid_to = normalize_ts(valid_to) if valid_to is not None else None
        knowledge_time = normalize_ts(knowledge_time)
        with self.database.connect() as db:
            if supersedes_id is not None:
                predecessor = db.execute(
                    "SELECT security_id,knowledge_time FROM universe_membership WHERE id=?",
                    (supersedes_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("superseded membership does not exist")
                if predecessor["security_id"] != security_id:
                    raise ValueError("membership supersession requires the same security")
                if (
                    predecessor["knowledge_time"] is not None
                    and knowledge_time < predecessor["knowledge_time"]
                ):
                    raise ValueError("membership supersession cannot backdate knowledge time")
            cursor = db.execute(
                """INSERT INTO universe_membership(
                    security_id,price,market_cap,avg_dollar_volume,price_eligible,
                    market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,
                    knowledge_time,pat_provenance,supersedes_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    security_id,
                    price,
                    market_cap,
                    avg_dollar_volume,
                    int(price_eligible),
                    int(market_cap_eligible),
                    int(liquidity_eligible),
                    int(eligible),
                    valid_from,
                    valid_to,
                    knowledge_time,
                    pat_provenance,
                    supersedes_id,
                ),
            )
            return int(cursor.lastrowid)

    def pit_valid(self, security_id: str, as_of: str) -> list[sqlite3.Row]:
        as_of = normalize_ts(as_of)
        with self.database.connect(read_only=True) as db:
            return list(
                db.execute(
                    """SELECT membership.* FROM universe_membership membership
                    WHERE membership.security_id=? AND membership.valid_from <= ?
                      AND (membership.valid_to IS NULL OR membership.valid_to > ?)
                      AND membership.knowledge_time <= ?
                      AND membership.pat_provenance IN (
                        'source_reported','derived_from_index')
                      AND NOT EXISTS (
                        SELECT 1 FROM universe_membership correction
                        WHERE correction.supersedes_id=membership.id
                          AND correction.knowledge_time <= ?
                          AND correction.pat_provenance IN (
                            'source_reported','derived_from_index'))
                    ORDER BY membership.knowledge_time, membership.id""",
                    (security_id, as_of, as_of, as_of, as_of),
                )
            )

    def current(self, security_id: str) -> list[sqlite3.Row]:
        """Return current-research facts, including historically unapproved provenance."""
        with self.database.connect(read_only=True) as db:
            return list(
                db.execute(
                    """SELECT membership.* FROM universe_membership membership
                    WHERE membership.security_id=? AND membership.valid_to IS NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM universe_membership correction
                        WHERE correction.supersedes_id=membership.id)
                    ORDER BY membership.knowledge_time, membership.id""",
                    (security_id,),
                )
            )


class SecurityIdentityStore:
    """Authoritative identity history; columns on security are convenience state only."""

    def __init__(self, database: ResearchDB):
        self.database = database

    def insert(
        self,
        *,
        security_id: str,
        event_type: str,
        old_value: str | None,
        new_value: str | None,
        event_time: str,
        public_available_time: str | None,
        pat_provenance: str,
    ) -> int:
        values: tuple[Any, ...] = (
            security_id,
            event_type,
            old_value,
            new_value,
            normalize_ts(event_time),
            normalize_ts(public_available_time) if public_available_time is not None else None,
            pat_provenance,
        )
        with self.database.connect() as db:
            cursor = db.execute(
                """INSERT INTO security_identity_event(
                    security_id,event_type,old_value,new_value,event_time,
                    public_available_time,pat_provenance) VALUES (?,?,?,?,?,?,?)""",
                values,
            )
            return int(cursor.lastrowid)

    def ticker_at(self, security_id: str, as_of: str) -> str:
        as_of = normalize_ts(as_of)
        with self.database.connect(read_only=True) as db:
            event = db.execute(
                """SELECT old_value,new_value FROM security_identity_event
                WHERE security_id=? AND event_type='ticker_change'
                  AND public_available_time IS NOT NULL AND public_available_time <= ?
                ORDER BY public_available_time DESC,id DESC LIMIT 1""",
                (security_id, as_of),
            ).fetchone()
            if event is not None:
                return str(event["new_value"])
            first = db.execute(
                """SELECT old_value FROM security_identity_event
                WHERE security_id=? AND event_type='ticker_change'
                ORDER BY public_available_time,id LIMIT 1""",
                (security_id,),
            ).fetchone()
            if first is not None:
                return str(first["old_value"])
            current = db.execute(
                "SELECT canonical_ticker FROM security WHERE security_id=?", (security_id,)
            ).fetchone()
            if current is None:
                raise KeyError(security_id)
            return str(current[0])

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
                    """WITH RECURSIVE visible_chain(root_id, descendant_id) AS (
                        SELECT candidate.id,candidate.id FROM universe_membership candidate
                        WHERE candidate.knowledge_time <= ?
                          AND NOT EXISTS (
                            SELECT 1 FROM universe_membership predecessor
                            WHERE predecessor.id=candidate.supersedes_id
                              AND predecessor.knowledge_time <= ?)
                        UNION ALL
                        SELECT chain.root_id, correction.id
                        FROM visible_chain chain JOIN universe_membership correction
                          ON correction.supersedes_id=chain.descendant_id
                        WHERE correction.knowledge_time <= ?
                    ), terminal(root_id,descendant_id) AS (
                        SELECT chain.root_id,chain.descendant_id FROM visible_chain chain
                        WHERE NOT EXISTS (
                            SELECT 1 FROM visible_chain child
                            JOIN universe_membership item ON item.id=child.descendant_id
                            WHERE child.root_id=chain.root_id
                              AND item.supersedes_id=chain.descendant_id)
                    )
                    SELECT DISTINCT membership.* FROM universe_membership membership
                    JOIN terminal ON terminal.descendant_id=membership.id
                    WHERE membership.security_id=? AND membership.valid_from <= ?
                      AND (membership.valid_to IS NULL OR membership.valid_to > ?)
                      AND membership.knowledge_time <= ?
                      AND membership.pat_provenance IN (
                        'source_reported','derived_from_index')
                    ORDER BY membership.knowledge_time, membership.id""",
                    (as_of, as_of, as_of, security_id, as_of, as_of, as_of),
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
    """Authoritative identity history; baseline/ticker_change share one supersession domain."""

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
        ingested_time: str | None = None,
        supersedes_id: int | None = None,
    ) -> int:
        event_time = normalize_ts(event_time)
        public_available_time = (
            normalize_ts(public_available_time) if public_available_time is not None else None
        )
        ingested_time = normalize_ts(ingested_time or public_available_time or event_time)
        if public_available_time is not None and public_available_time > ingested_time:
            raise ValueError("public_available_time cannot follow ingested_time")
        values: tuple[Any, ...] = (
            security_id,
            event_type,
            old_value,
            new_value,
            event_time,
            public_available_time,
            pat_provenance,
            ingested_time,
            supersedes_id,
        )
        with self.database.connect() as db:
            if supersedes_id is not None:
                predecessor = db.execute(
                    "SELECT security_id,event_type,public_available_time "
                    "FROM security_identity_event WHERE id=?",
                    (supersedes_id,),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("superseded identity event does not exist")
                if predecessor["security_id"] != security_id:
                    raise ValueError("identity supersession requires the same security")
                ticker_domain = {"baseline", "ticker_change"}
                predecessor_type = predecessor["event_type"]
                if predecessor_type != event_type and not (
                    predecessor_type in ticker_domain and event_type in ticker_domain
                ):
                    raise ValueError("identity supersession requires a compatible event domain")
                if public_available_time is None or (
                    predecessor["public_available_time"] is not None
                    and public_available_time < predecessor["public_available_time"]
                ):
                    raise ValueError("identity supersession cannot backdate knowledge time")
            cursor = db.execute(
                """INSERT INTO security_identity_event(
                    security_id,event_type,old_value,new_value,event_time,
                    public_available_time,pat_provenance,ingested_time,supersedes_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                values,
            )
            return int(cursor.lastrowid)

    def ticker_at(self, security_id: str, as_of: str) -> str | None:
        with self.database.connect(read_only=True) as db:
            return self.ticker_at_connection(db, security_id, as_of)

    @staticmethod
    def ticker_at_connection(
        db: sqlite3.Connection, security_id: str, as_of: str
    ) -> str | None:
        """Resolve a PIT ticker within the caller's existing SQLite snapshot."""
        as_of = normalize_ts(as_of)
        event = db.execute(
            """WITH RECURSIVE visible_chain(root_id,descendant_id) AS (
                    SELECT candidate.id,candidate.id FROM security_identity_event candidate
                    WHERE candidate.public_available_time IS NOT NULL
                      AND candidate.event_type IN ('baseline','ticker_change')
                      AND candidate.public_available_time <= ?
                      AND candidate.event_time <= ?
                      AND NOT EXISTS (
                        SELECT 1 FROM security_identity_event predecessor
                        WHERE predecessor.id=candidate.supersedes_id
                          AND predecessor.event_type IN ('baseline','ticker_change')
                          AND predecessor.public_available_time IS NOT NULL
                          AND predecessor.public_available_time <= ?
                          AND predecessor.event_time <= ?)
                    UNION ALL
                    SELECT chain.root_id,successor.id FROM visible_chain chain
                    JOIN security_identity_event successor
                      ON successor.supersedes_id=chain.descendant_id
                     AND successor.event_type IN ('baseline','ticker_change')
                    WHERE successor.public_available_time IS NOT NULL
                      AND successor.public_available_time <= ?
                      AND successor.event_time <= ?
                ), terminal(root_id,descendant_id) AS (
                    SELECT chain.root_id,chain.descendant_id FROM visible_chain chain
                    WHERE NOT EXISTS (
                        SELECT 1 FROM visible_chain child
                        JOIN security_identity_event item ON item.id=child.descendant_id
                        WHERE child.root_id=chain.root_id
                          AND item.event_type IN ('baseline','ticker_change')
                          AND item.supersedes_id=chain.descendant_id)
                )
                SELECT identity.new_value FROM security_identity_event identity
                JOIN terminal ON terminal.descendant_id=identity.id
                WHERE identity.security_id=?
                  AND identity.event_type IN ('baseline','ticker_change')
                  AND identity.pat_provenance IN ('source_reported','derived_from_index')
                ORDER BY identity.event_time DESC,identity.public_available_time DESC,
                    identity.id DESC LIMIT 1""",
            (as_of, as_of, as_of, as_of, as_of, as_of, security_id),
        ).fetchone()
        return str(event["new_value"]) if event is not None else None

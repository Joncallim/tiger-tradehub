"""Security bootstrap for the BOOTSTRAP_COHORT sample (Finding 1).

Nothing in the repo creates security/universe_membership/baseline identity
rows -- Tiingo/SEC ingestion assumes identity already resolved. This module
inserts baseline rows for the frozen sample via the EXISTING
UniverseMembershipStore / SecurityIdentityStore classes, unmodified.

Explicit PIT limitation (documented, never hidden): knowledge_time is the
file-retrieval time, NOT a true historical constituent-index date. The
cohort is labeled BOOTSTRAP_COHORT; deeper historical PIT-correct
membership reconstruction is out of scope and may legitimately yield
INSUFFICIENT DATA for pre-bootstrap dates.
"""

from __future__ import annotations

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.universe import SecurityIdentityStore, UniverseMembershipStore


def bootstrap_security_rows(
    research_db: ResearchDB,
    *,
    selected: list[dict[str, str]],
    knowledge_time: str | None = None,
) -> dict[str, int]:
    """Insert baseline security + identity + universe_membership rows.

    security_id = zero-padded CIK (matching the SEC/Tiingo adapters'
    internal CIK convention). The identity event is event_type='baseline'
    with new_value=ticker; universe membership is eligible=1 with
    price/market_cap/liquidity NULL (filled later by Tiingo).

    Returns counts inserted. Idempotent: re-running on the same research.db
    is a no-op for rows that already exist.
    """
    knowledge_time = knowledge_time or utc_now()
    identity_store = SecurityIdentityStore(research_db)
    membership_store = UniverseMembershipStore(research_db)
    counts = {"security": 0, "identity_event": 0, "membership": 0}

    with research_db.connect() as conn:
        for row in selected:
            security_id = row["cik"]
            ticker = row["ticker"]
            cursor = conn.execute(
                "INSERT OR IGNORE INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    security_id,
                    ticker,
                    "US",
                    row.get("title", ""),
                    None,
                    None,
                    "SUPPORTED",
                    knowledge_time,
                    None,
                ),
            )
            counts["security"] += cursor.rowcount
        conn.execute(
            "INSERT OR IGNORE INTO evidence_source VALUES (?,?,?,?,?)",
            ("sec_index", "regulatory_index", 1, "company_tickers bootstrap", "derived_from_index"),
        )

    with research_db.connect(read_only=True) as conn:
        existing_ids = {
            row[0]
            for row in conn.execute(
                "SELECT security_id FROM security_identity_event WHERE event_type='baseline'"
            ).fetchall()
        }
    for row in selected:
        security_id = row["cik"]
        if security_id not in existing_ids:
            identity_store.insert(
                security_id=security_id,
                event_type="baseline",
                old_value=None,
                new_value=row["ticker"],
                event_time=knowledge_time,
                public_available_time=knowledge_time,
                pat_provenance="derived_from_index",
                ingested_time=knowledge_time,
            )
            counts["identity_event"] += 1

    for row in selected:
        security_id = row["cik"]
        if not membership_store.pit_valid(security_id, knowledge_time):
            membership_store.insert(
                security_id=security_id,
                price=None,
                market_cap=None,
                avg_dollar_volume=None,
                price_eligible=False,
                market_cap_eligible=False,
                liquidity_eligible=False,
                eligible=True,
                valid_from=knowledge_time,
                valid_to=None,
                knowledge_time=knowledge_time,
                pat_provenance="derived_from_index",
            )
            counts["membership"] += 1
    return counts

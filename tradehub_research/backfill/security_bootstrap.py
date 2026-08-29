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

Reconciliation (2026-08-28): the first live bootstrap run inserted a
ticker list that did NOT match the frozen hash-selected sample (the
security table's canonical tickers were the alphabetical head of
company_tickers.json, not the sample). ``reconcile_cohort_identity``
aligns the identity layer to the frozen universe_sample append-only-safe:
- security rows are plain-table rows (updatable): missing cohort CIKs are
  inserted, mismatched canonical tickers corrected (with a ticker_change
  identity event);
- security_identity_event is append-only: missing baselines inserted,
  corrections appended as superseding ticker_change events;
- universe_membership is append-only: every non-cohort membership is
  superseded by an eligible=0 correction row (the erroneous securities can
  never screen again) and every cohort CIK without a terminal eligible
  membership gets one.
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


def _cohort_canonical_tickers(selected: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """CIK -> {ticker, title} -- first ticker in the frozen sample order wins."""
    canonical: dict[str, dict[str, str]] = {}
    for row in selected:
        cik = str(row["cik"]).zfill(10)
        canonical.setdefault(cik, {"ticker": row["ticker"], "title": row.get("title", "")})
    return canonical


def reconcile_cohort_identity(
    research_db: ResearchDB,
    *,
    selected: list[dict[str, str]],
    knowledge_time: str | None = None,
) -> dict[str, int]:
    """Align security/identity/membership rows to the frozen cohort.

    Append-only-safe correction for the misaligned first bootstrap (see
    module docstring). Returns counts of rows inserted/updated/superseded.
    Idempotent: re-running converges to the same aligned state.
    """
    knowledge_time = knowledge_time or utc_now()
    canonical = _cohort_canonical_tickers(selected)
    counts = {
        "security_inserted": 0,
        "ticker_corrected": 0,
        "baseline_added": 0,
        "correction_added": 0,
        "membership_added": 0,
        "membership_superseded": 0,
    }

    with research_db.connect() as conn:
        existing = {
            str(row[0]): str(row[1])
            for row in conn.execute("SELECT security_id, canonical_ticker FROM security").fetchall()
        }
        # 1. security rows: insert missing, correct mismatched canonical tickers.
        for cik, meta in canonical.items():
            if cik not in existing:
                conn.execute(
                    "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        cik,
                        meta["ticker"],
                        "US",
                        meta["title"],
                        None,
                        None,
                        "SUPPORTED",
                        knowledge_time,
                        None,
                    ),
                )
                counts["security_inserted"] += 1
            elif existing[cik] != meta["ticker"]:
                conn.execute(
                    "UPDATE security SET canonical_ticker=? WHERE security_id=?",
                    (meta["ticker"], cik),
                )
                counts["ticker_corrected"] += 1

        # 2. identity events: baseline for missing CIKs; superseding
        #    ticker_change correction for mismatched canonical tickers.
        events = {
            str(row[0]): dict(row)
            for row in conn.execute(
                "SELECT id, security_id, event_type, new_value FROM security_identity_event"
            ).fetchall()
        }
        terminal: dict[str, int] = {}
        for event_id, event in events.items():
            terminal[event["security_id"]] = event_id  # last-inserted wins (append-only order)
        for cik, meta in canonical.items():
            if cik not in terminal:
                conn.execute(
                    "INSERT INTO security_identity_event "
                    "(security_id,event_type,old_value,new_value,event_time,"
                    "public_available_time,pat_provenance,ingested_time,supersedes_id) "
                    "VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (
                        cik,
                        "baseline",
                        None,
                        meta["ticker"],
                        knowledge_time,
                        knowledge_time,
                        "derived_from_index",
                        knowledge_time,
                    ),
                )
                counts["baseline_added"] += 1
            elif str(events[terminal[cik]]["new_value"]) != meta["ticker"]:
                conn.execute(
                    "INSERT INTO security_identity_event "
                    "(security_id,event_type,old_value,new_value,event_time,"
                    "public_available_time,pat_provenance,ingested_time,supersedes_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        cik,
                        "ticker_change",
                        str(events[terminal[cik]]["new_value"]),
                        meta["ticker"],
                        knowledge_time,
                        knowledge_time,
                        "derived_from_index",
                        knowledge_time,
                        terminal[cik],
                    ),
                )
                counts["correction_added"] += 1

    # 3. memberships: supersede every non-cohort eligible membership with an
    #    eligible=0 correction; add eligible memberships for cohort CIKs
    #    lacking a terminal one.
    membership_store = UniverseMembershipStore(research_db)
    with research_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT id, security_id, eligible FROM universe_membership "
            "WHERE eligible=1 AND NOT EXISTS ("
            "  SELECT 1 FROM universe_membership successor "
            "  WHERE successor.supersedes_id=universe_membership.id)"
            "ORDER BY id"
        ).fetchall()
    cohort_ciks = set(canonical)
    for row in rows:
        if str(row["security_id"]) not in cohort_ciks and int(row["eligible"]) == 1:
            membership_store.insert(
                security_id=str(row["security_id"]),
                price=None,
                market_cap=None,
                avg_dollar_volume=None,
                price_eligible=False,
                market_cap_eligible=False,
                liquidity_eligible=False,
                eligible=False,
                valid_from=knowledge_time,
                valid_to=None,
                knowledge_time=knowledge_time,
                pat_provenance="derived_from_index",
                supersedes_id=int(row["id"]),
            )
            counts["membership_superseded"] += 1
    for cik in sorted(cohort_ciks):
        if not membership_store.pit_valid(cik, knowledge_time):
            membership_store.insert(
                security_id=cik,
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
            counts["membership_added"] += 1
    return counts

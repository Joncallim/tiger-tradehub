"""Security bootstrap reconciliation: aligns identity to the frozen cohort."""

from __future__ import annotations

from tradehub_research.backfill.security_bootstrap import (
    bootstrap_security_rows,
    reconcile_cohort_identity,
)
from tradehub_research.db import ResearchDB

COHORT = [
    {"cik": "0000000001", "ticker": "AAAA", "title": "Aaa Co"},
    {"cik": "0000000002", "ticker": "AAAB", "title": "Aab Co"},
    {"cik": "0000000002", "ticker": "AAAC", "title": "Aab Pref"},
    {"cik": "0000000003", "ticker": "AAAD", "title": "Aad Co"},
]


def _misaligned_research_db(tmp_path) -> ResearchDB:
    """Simulate the 2026-08-27 defect: wrong (alphabetical-head) ticker list
    bootstrapped instead of the frozen hash-selected cohort."""
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    wrong = [
        {"cik": "0000000001", "ticker": "AAAA", "title": "Aaa Co"},  # coincidentally right
        {"cik": "0000000002", "ticker": "AABA", "title": "Aab Co"},  # wrong canonical
        {"cik": "0000000004", "ticker": "ZZZZ", "title": "Not In Cohort"},
    ]
    bootstrap_security_rows(research_db, selected=wrong, knowledge_time="2026-08-27T00:00:00Z")
    return research_db


def test_reconcile_aligns_identity_to_cohort(tmp_path):
    research_db = _misaligned_research_db(tmp_path)

    counts = reconcile_cohort_identity(
        research_db, selected=COHORT, knowledge_time="2026-08-28T00:00:00Z"
    )

    assert counts["security_inserted"] == 1  # only 0000000003 missing
    assert counts["ticker_corrected"] == 1  # 0000000002: AABA -> AAAB
    assert counts["baseline_added"] == 1  # 0000000003
    assert counts["correction_added"] == 1  # 0000000002 ticker change
    assert counts["membership_superseded"] == 1  # 0000000004 -> ineligible
    assert counts["membership_added"] == 1  # 0000000003

    with research_db.connect(read_only=True) as conn:
        ticker = conn.execute(
            "SELECT canonical_ticker FROM security WHERE security_id='0000000002'"
        ).fetchone()[0]
        assert ticker == "AAAB"
        terminal_eligible = conn.execute(
            "SELECT COUNT(*) FROM universe_membership m WHERE m.eligible=1 AND NOT EXISTS "
            "(SELECT 1 FROM universe_membership s WHERE s.supersedes_id=m.id)"
        ).fetchone()[0]
        assert terminal_eligible == 3  # 0001, 0002, 0003 (0004 superseded to 0)
        correction = conn.execute(
            "SELECT new_value FROM security_identity_event WHERE security_id='0000000002' "
            "AND event_type='ticker_change'"
        ).fetchone()
        assert correction[0] == "AAAB"


def test_reconcile_is_idempotent(tmp_path):
    research_db = _misaligned_research_db(tmp_path)
    reconcile_cohort_identity(research_db, selected=COHORT, knowledge_time="2026-08-28T00:00:00Z")
    second = reconcile_cohort_identity(
        research_db, selected=COHORT, knowledge_time="2026-08-28T01:00:00Z"
    )
    assert second["security_inserted"] == 0
    assert second["ticker_corrected"] == 0
    assert second["correction_added"] == 0
    assert second["membership_superseded"] == 0
    assert second["membership_added"] == 0
    with research_db.connect(read_only=True) as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM security_identity_event WHERE event_type='ticker_change'"
        ).fetchone()[0]
    assert events == 1  # no duplicate corrections

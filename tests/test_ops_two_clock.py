"""Two-clock contract regression: evidence_as_of vs decision_as_of.

evidence_as_of = the frozen pipeline market/evidence cutoff. It bounds every
packed evidence row and every model fact (PIT) and is never moved forward just
because committee models finish later.

decision_as_of = the actual operational decision time. It must be >= every
persisted score's computed_at so an asynchronously completed committee score
remains visible to Phase 3. The contract is never satisfied by backdating a
score's computed_at.
"""

from __future__ import annotations

import pytest

from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security
from tradehub_research.db import ResearchDB
from tradehub_research.ops.decision_pipeline import run_portfolio_decision

T0_EVIDENCE = "2026-09-10T20:15:00Z"  # pipeline evidence cutoff
T1_SCORE = "2026-09-10T22:40:00Z"  # async committee score persisted later
T2_DECISION = "2026-09-10T23:05:00Z"  # operational decision time


def _database_with_async_score(tmp_path) -> ResearchDB:
    """T0 evidence cutoff, score persisted at T1 > T0."""
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as conn:
        seed_security(conn, "S1")
        seed_pipeline_run(conn, "R1", T0_EVIDENCE)
        seed_score(
            conn,
            pipeline_run_id="R1",
            security_id="S1",
            run_as_of=T1_SCORE,  # helpers bind score_snapshot.computed_at to this
        )
    return database


def test_decision_before_persisted_score_is_refused(tmp_path):
    """A decision time earlier than the async score must fail loudly."""
    database = _database_with_async_score(tmp_path)
    with pytest.raises(ValueError, match="newer than decision_as_of"):
        run_portfolio_decision(
            database,
            pipeline_run_id="R1",
            decision_as_of=T0_EVIDENCE,
            evidence_as_of=T0_EVIDENCE,
        )


def test_async_score_visible_at_later_decision_time(tmp_path):
    """T0 < T1 <= T2: the async score is visible and both clocks are reported."""
    assert T0_EVIDENCE < T1_SCORE <= T2_DECISION
    database = _database_with_async_score(tmp_path)
    result = run_portfolio_decision(
        database,
        pipeline_run_id="R1",
        decision_as_of=T2_DECISION,
        evidence_as_of=T0_EVIDENCE,
    )
    # The two-clock guard must not fire: the score is no longer "in the future".
    assert result["decision_as_of"] == T2_DECISION
    assert result["evidence_as_of"] == T0_EVIDENCE
    # Any further blocker is downstream of score visibility, never the clock.
    assert result["status"] != "BLOCKED_NO_VALID_SCORE"


def test_score_evidence_stays_bounded_at_the_frozen_cutoff(tmp_path):
    """The score is bound to its PIT pack; moving decision time must not move it."""
    database = _database_with_async_score(tmp_path)
    with database.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT s.computed_at, c.pipeline_run_id, p.as_of AS evidence_as_of "
            "FROM score_snapshot s "
            "JOIN committee_run c ON c.committee_run_id=s.committee_run_id "
            "JOIN pipeline_run p ON p.run_id=c.pipeline_run_id"
        ).fetchone()
    assert row["computed_at"] == T1_SCORE
    assert row["evidence_as_of"] == T0_EVIDENCE
    assert row["computed_at"] != row["evidence_as_of"]


def test_decision_cannot_precede_evidence_cutoff(tmp_path):
    """decision_as_of < evidence_as_of is a hard error, not a silent clamp."""
    database = _database_with_async_score(tmp_path)
    with pytest.raises(ValueError, match="must not precede evidence_as_of"):
        run_portfolio_decision(
            database,
            pipeline_run_id="R1",
            decision_as_of="2026-09-10T01:00:00Z",
            evidence_as_of=T0_EVIDENCE,
        )

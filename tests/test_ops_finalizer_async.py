"""Integration regression for the SCHEDULED async committee finalizer.

This exercises ``finalize_async_committee_decisions`` itself (the 5-minute
timer path) rather than the helper, and pins the two-clock + idempotency
contracts:

* evidence_as_of stays at the pipeline cutoff T0;
* an asynchronously persisted score (T1/T2 > T0) is still visible;
* the same durable score set yields exactly ONE logical portfolio decision;
* a second invocation is REUSED with no duplicate observation/proposal;
* a genuinely NEW score set yields exactly one new invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security
from tradehub_research.db import ResearchDB
from tradehub_research.ops.decision_pipeline import (
    current_score_set,
    finalize_async_committee_decisions,
)

T0_EVIDENCE = "2026-09-10T20:15:00Z"  # pipeline market/evidence cutoff
T1_FIRST_SCORE = "2026-09-10T22:40:00Z"  # first async score persisted
T2_SECOND_SCORE = "2026-09-10T23:10:00Z"  # second async score persisted
T3_THIRD_SCORE = "2026-09-11T01:30:00Z"  # a genuinely new score set arrives

PIPELINE_RUN = "R1"


def _handoff(tmp_path: Path, as_of: str = T0_EVIDENCE) -> Path:
    """A valid, credential-free, empty-book PAPER handoff known at as_of."""
    path = tmp_path / "handoff.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "paper-portfolio-handoff-v2",
                "account_type": "PAPER",
                "environment": "PAPER_SANDBOX",
                "account_status": "Funded",
                "as_of": as_of,
                "positions": [],
                "cash": "1000000.00",
                "nav": "1000000.00",
            }
        )
    )
    return path


def _database(tmp_path: Path, *, securities: tuple[str, ...], score_times: tuple[str, ...]):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as conn:
        seed_pipeline_run(conn, PIPELINE_RUN, T0_EVIDENCE)
        for security_id, scored_at in zip(securities, score_times, strict=True):
            seed_security(conn, security_id)
            seed_score(
                conn,
                pipeline_run_id=PIPELINE_RUN,
                security_id=security_id,
                run_as_of=scored_at,  # binds score_snapshot.computed_at
            )
    return database


def _counts(database: ResearchDB) -> tuple[int, int, int]:
    with database.connect(read_only=True) as conn:
        runs = conn.execute("SELECT count(*) FROM portfolio_run").fetchone()[0]
        observations = conn.execute("SELECT count(*) FROM portfolio_state_observation").fetchone()[
            0
        ]
        proposals = conn.execute("SELECT count(*) FROM trade_proposal").fetchone()[0]
    return runs, observations, proposals


def test_same_score_set_is_finalized_once_and_then_reused(tmp_path):
    assert T0_EVIDENCE < T1_FIRST_SCORE <= T2_SECOND_SCORE
    database = _database(
        tmp_path,
        securities=("S1", "S2"),
        score_times=(T1_FIRST_SCORE, T2_SECOND_SCORE),
    )
    handoff = _handoff(tmp_path)
    inbox = tmp_path / "inbox"

    first = finalize_async_committee_decisions(database, inbox=inbox, handoff=handoff)
    entry = first["finalized"][0]

    # Score visibility: the async scores are newer than the evidence cutoff.
    assert entry["evidence_as_of"] == T0_EVIDENCE
    assert entry["score_ready_at"] == T2_SECOND_SCORE
    # The epoch is bound to the durable score set, not to a fresh wall clock.
    assert entry["decision_as_of"] == T2_SECOND_SCORE
    assert entry["status"] == "HEALTHY_ZERO_ACTION"
    assert entry["portfolio_run"]["observation_count"] == 2

    runs_after_first, observations_after_first, _ = _counts(database)
    assert runs_after_first == 1
    assert observations_after_first == 2

    # Second tick over the SAME durable score set must reuse, never duplicate.
    second = finalize_async_committee_decisions(database, inbox=inbox, handoff=handoff)
    reused = second["finalized"][0]
    assert reused["status"] == "REUSED"
    assert reused["decision_as_of"] == T2_SECOND_SCORE
    assert reused["portfolio_run"]["run_id"] == entry["portfolio_run"]["run_id"]

    runs_after_second, observations_after_second, proposals_after_second = _counts(database)
    assert runs_after_second == runs_after_first == 1
    assert observations_after_second == observations_after_first == 2
    assert proposals_after_second == 0


def test_new_score_set_creates_exactly_one_new_invocation(tmp_path):
    database = _database(
        tmp_path,
        securities=("S1", "S2"),
        score_times=(T1_FIRST_SCORE, T2_SECOND_SCORE),
    )
    handoff = _handoff(tmp_path)
    inbox = tmp_path / "inbox"

    finalize_async_committee_decisions(database, inbox=inbox, handoff=handoff)
    runs_before, _, _ = _counts(database)

    before_set = current_score_set(database, PIPELINE_RUN)
    with database.connect() as conn:
        seed_security(conn, "S3")
        seed_score(
            conn,
            pipeline_run_id=PIPELINE_RUN,
            security_id="S3",
            run_as_of=T3_THIRD_SCORE,
        )
    after_set = current_score_set(database, PIPELINE_RUN)

    # The score set genuinely changed, so this is not a duplicate.
    assert after_set["score_set_hash"] != before_set["score_set_hash"]
    assert after_set["score_ready_at"] == T3_THIRD_SCORE

    third = finalize_async_committee_decisions(database, inbox=inbox, handoff=handoff)
    entry = third["finalized"][0]
    assert entry["status"] != "REUSED"
    assert entry["evidence_as_of"] == T0_EVIDENCE
    assert entry["decision_as_of"] == T3_THIRD_SCORE

    runs_after, _, proposals_after = _counts(database)
    assert runs_after == runs_before + 1  # exactly one new invocation
    assert proposals_after == 0

    # And that new epoch is itself now durable/idempotent.
    again = finalize_async_committee_decisions(database, inbox=inbox, handoff=handoff)
    assert again["finalized"][0]["status"] == "REUSED"
    assert _counts(database)[0] == runs_after


def test_missing_handoff_does_not_invent_a_clock(tmp_path):
    """No usable handoff ⇒ fail closed with no epoch and no portfolio run."""
    database = _database(
        tmp_path,
        securities=("S1",),
        score_times=(T1_FIRST_SCORE,),
    )
    missing = tmp_path / "does-not-exist.json"
    result = finalize_async_committee_decisions(database, inbox=tmp_path / "inbox", handoff=missing)
    entry = result["finalized"][0]
    assert entry["status"] == "BLOCKED_FINALIZER_ERROR"
    assert "no usable sanitized PAPER handoff" in entry["reason"]
    assert _counts(database)[0] == 0

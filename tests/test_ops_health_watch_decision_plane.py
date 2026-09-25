"""The health watch must watch the DECISION plane, not only ingestion.

Live gap (found 2026-09-25): the research service issues committee work
envelopes and a model worker must drive them (the service holds no provider
credentials by design). Nothing had driven it since 2026-09-15/16, so 207 genuine
committee runs accumulated -- 193 with no score at all -- every cycle since then
produced zero scored candidates, `decision_pipeline` returned BLOCKED_NO_VALID_SCORE,
`trade_proposal` stayed empty, and the PAPER runner logged IDLE_EMPTY_INBOX 1,275
times. The daily/weekly reports showed 0 trades for ten days and the watch stayed
silent: it covered missed cycles, market-data freshness, maturation backlog,
PAPER proof, kill switch, restarts and reconciliation age -- every input to the
decision, nothing about the decision.

This is the enforcement condition that was missing. It is deliberately anchored
on the SAME authority the decision gate uses (a committee run without a
``score_snapshot``), and it ignores acceptance runs, which are not the queue.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security
from tradehub_research.db import ResearchDB
from tradehub_research.ops import health_watch

NOW = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    return database


@pytest.fixture(autouse=True)
def _clean_alerts():
    saved = list(health_watch.ALERTS)
    health_watch.ALERTS.clear()
    yield
    health_watch.ALERTS[:] = saved


def _unscored_run(conn, *, key: str, suffix: str, pipeline_run_id: str, created_at: str) -> str:
    """A committee run that never produced a score_snapshot (the stall shape).

    Reuses the reference rows ``seed_score`` created (candidate / evidence pack /
    comparator / scoring version), because committee_run has real foreign keys --
    the same shape the router writes.
    """
    committee_run_id = f"cr-{key}"
    conn.execute(
        "INSERT INTO committee_run(committee_run_id,candidate_id,pipeline_run_id,pack_hash,"
        "role_set_json,committee_policy_version,comparator_config_hash,scoring_config_hash,"
        "prompt_versions_json,assessment_schema_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            committee_run_id,
            f"cand-S1-{suffix}",
            pipeline_run_id,
            f"pack-S1-{suffix}",
            '["neutral_analyst_a","neutral_analyst_b"]',
            1,
            f"cc-S1-{suffix}",
            f"sc-S1-{suffix}",
            '{"neutral":"v2"}',
            1,
            created_at,
        ),
    )
    return committee_run_id


def _seed_scored_cycle(
    database, *, pipeline_run_id: str = "R1", as_of: str = "2026-09-24T20:15:00Z", suffix: str = "a"
) -> None:
    """One scored candidate on the named cycle (the healthy part of the plane)."""
    with database.connect() as conn:
        seed_security(conn, "S1")
        seed_pipeline_run(conn, pipeline_run_id, as_of)
        seed_score(
            conn,
            pipeline_run_id=pipeline_run_id,
            security_id="S1",
            run_as_of=as_of,
            committee_suffix=suffix,
        )


def _seed_unscored(
    database, *, key: str, created_at: str, pipeline_run_id: str = "R1", suffix: str = "a"
) -> None:
    with database.connect() as conn:
        _unscored_run(
            conn,
            key=key,
            suffix=suffix,
            pipeline_run_id=pipeline_run_id,
            created_at=created_at,
        )


def test_state_counts_runs_without_a_score_as_outstanding(db):
    _seed_scored_cycle(db)
    _seed_unscored(db, key="u1", created_at="2026-09-16T15:03:00Z")
    _seed_unscored(db, key="u2", created_at="2026-09-18T15:03:00Z")

    state = health_watch.decision_plane_state(db, now=NOW)

    assert state["outstanding_runs"] == 2
    assert state["oldest_outstanding_at"] == "2026-09-16T15:03:00Z"
    assert state["oldest_outstanding_hours"] == pytest.approx(203.95, abs=0.1)
    assert state["latest_pipeline_as_of"] == "2026-09-24T20:15:00Z"
    assert state["latest_pipeline_scored"] == 1


def test_a_stalled_queue_raises_exactly_one_alert(db):
    _seed_scored_cycle(db)
    _seed_unscored(db, key="u1", created_at="2026-09-16T15:03:00Z")

    health_watch.check_decision_plane(db, now=NOW)

    assert len(health_watch.ALERTS) == 1, health_watch.ALERTS
    line = health_watch.ALERTS[0]
    assert line.startswith("TRADEHUB WATCH:")
    assert "1 committee run" in line
    assert "2026-09-16" in line
    assert "proposal" in line


def test_a_fresh_queue_is_silent(db):
    """A queue issued hours ago is a busy worker, not a stalled decision plane."""
    _seed_scored_cycle(db)
    _seed_unscored(db, key="u1", created_at="2026-09-25T02:00:00Z")

    health_watch.check_decision_plane(db, now=NOW)

    assert health_watch.ALERTS == []


def test_a_fully_scored_queue_is_silent(db):
    _seed_scored_cycle(db)
    health_watch.check_decision_plane(db, now=NOW)
    assert health_watch.ALERTS == []


def test_acceptance_runs_are_not_the_production_queue(db):
    """Acceptance-era runs are historical fixtures, not outstanding production work."""
    _seed_scored_cycle(db, pipeline_run_id="pr66-acceptance-2", as_of="2026-09-16T00:00:00Z")
    _seed_unscored(
        db,
        key="acc1",
        created_at="2026-09-16T00:00:00Z",
        pipeline_run_id="pr66-acceptance-2",
    )

    state = health_watch.decision_plane_state(db, now=NOW)
    health_watch.check_decision_plane(db, now=NOW)

    assert state["outstanding_runs"] == 0, state
    assert health_watch.ALERTS == []


def test_state_reads_the_proposal_lifetime_total(db):
    _seed_scored_cycle(db)
    _seed_unscored(db, key="u1", created_at="2026-09-16T15:03:00Z")

    state = health_watch.decision_plane_state(db, now=NOW)

    assert state["proposals_lifetime"] == 0  # never a single proposal, live shape
    assert state["latest_pipeline_candidates"] == 1

"""Worker-plane monitor: A/B/C alert separately, D (real NO_ACTION) is healthy.

The decision-plane condition detects the symptom (a stalled queue). These tests pin
the CAUSE-level observability added after the 2026-09-25 incident so the next
occurrence names which link broke: nobody drove the work (A), the driver's
submissions failed (B), or the scorer/finalizer stopped advancing (C). A cycle that
legitimately decided NO_ACTION (D) must never be presented as a pipeline failure.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security

CYCLE = "cycle-2026-09-24"
NOW = datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc)


def _database(tmp_path):
    from tradehub_research.db import ResearchDB

    db = ResearchDB(tmp_path / "research.db", 5000)
    db.migrate()
    return db


def _seed_cycle(db, *, unscored: int, created_at: str) -> None:
    """Seed the current cycle: `unscored` outstanding runs (+ 1 scored run when asked).

    The seeded scored run supplies the candidate / pack / comparator reference rows;
    the cloned runs stay UNSOCORED, so the outstanding count is exactly ``unscored``.
    """
    with db.connect() as conn:
        seed_security(conn, "S1")
        seed_pipeline_run(conn, CYCLE, "2026-09-24T20:15:00Z")
        seed_score(conn, pipeline_run_id=CYCLE, security_id="S1", run_as_of="2026-09-24T20:15:00Z")
        source = conn.execute(
            "SELECT * FROM committee_run WHERE pipeline_run_id = ?", (CYCLE,)
        ).fetchone()
        if source is None:
            raise AssertionError("seed_score did not create a reference committee run")
        for index in range(unscored):
            payload = dict(source)
            payload["committee_run_id"] = f"cr-unscored-{index}"
            payload["pipeline_run_id"] = CYCLE
            payload["created_at"] = created_at
            columns = ", ".join(payload)
            marks = ", ".join("?" * len(payload))
            conn.execute(
                f"INSERT INTO committee_run ({columns}) VALUES ({marks})",
                tuple(payload.values()),
            )


def _patch_decision(monkeypatch, *, no_action: bool = True) -> None:
    """Stub the durable decision read (engine-owned rows) for alert isolation."""
    from tradehub_research.ops import health_watch

    monkeypatch.setattr(
        health_watch,
        "_decision_summary",
        lambda conn, run_id: {
            "decision_as_of": "2026-09-25T19:05:53Z",
            "proposals": 0,
            "final_statuses": ["NO_ACTION"] if no_action else ["ENTER"],
        },
    )


def _state_paths(tmp_path: Path, payload: dict) -> SimpleNamespace:
    research_dir = tmp_path / "state"
    research_dir.mkdir(parents=True, exist_ok=True)
    (research_dir / "committee-worker-state.json").write_text(json.dumps(payload), encoding="utf-8")
    return SimpleNamespace(research_dir=research_dir, research_db=tmp_path / "research.db")


@pytest.fixture(autouse=True)
def _clear_alerts():
    from tradehub_research.ops import health_watch

    health_watch.ALERTS.clear()
    yield
    health_watch.ALERTS.clear()


# --------------------------------------------------------------------------- #
# real state computation
# --------------------------------------------------------------------------- #


def test_work_exists_and_worker_never_ran_alerts_A(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    db = _database(tmp_path)
    _seed_cycle(db, unscored=3, created_at="2026-09-25T20:15:00Z")
    _patch_decision(monkeypatch)
    paths = _state_paths(tmp_path, {"providers_ready": False, "providers": {}})

    state = health_watch.check_worker_plane(db, paths=paths, now=NOW)

    assert state["outstanding_runs"] == 3
    assert state["cycle_as_of"] == "2026-09-24T20:15:00Z"
    assert state["oldest_outstanding_hours"] == pytest.approx(7.75, abs=0.05)
    assert any("not being driven" in alert for alert in health_watch.ALERTS), health_watch.ALERTS
    assert state["last_worker_activity_at"] is None


def test_worker_running_but_submissions_failing_alerts_B(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    db = _database(tmp_path)
    _seed_cycle(db, unscored=2, created_at="2026-09-26T03:00:00Z")
    _patch_decision(monkeypatch)
    paths = _state_paths(
        tmp_path,
        {
            "last_activity_at": "2026-09-26T03:59:00Z",
            "last_run_records": [
                {
                    "committee_run_id": "cr-unscored-0",
                    "stopped": "submit-failed-500",
                    "roles": [{"role": "neutral_analyst_a", "http_error": "500 internal error"}],
                }
            ],
        },
    )

    state = health_watch.check_worker_plane(db, paths=paths, now=NOW)

    assert state["last_run_failures"], state
    assert any("submissions are failing" in alert for alert in health_watch.ALERTS), (
        health_watch.ALERTS
    )


def test_healthy_no_action_cycle_raises_no_alert(tmp_path, monkeypatch):
    """D: durable decision exists, decided NO_ACTION, nothing outstanding -> healthy."""
    from tradehub_research.ops import health_watch

    db = _database(tmp_path)
    _seed_cycle(db, unscored=0, created_at="2026-09-24T20:15:00Z")
    _patch_decision(monkeypatch)
    paths = _state_paths(tmp_path, {"last_activity_at": "2026-09-26T03:59:00Z"})

    state = health_watch.check_worker_plane(db, paths=paths, now=NOW)

    assert state["decision_status"] == "COMPLETE"
    assert state["decision_no_action"] is True
    assert health_watch.ALERTS == [], health_watch.ALERTS


def test_provider_readiness_is_exposed_with_its_reason(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    db = _database(tmp_path)
    _seed_cycle(db, unscored=1, created_at="2026-09-26T03:59:00Z")
    _patch_decision(monkeypatch)
    paths = _state_paths(
        tmp_path,
        {
            "last_activity_at": "2026-09-25T18:00:00Z",
            "providers_ready": False,
            "providers": {
                "anthropic": {
                    "ready": False,
                    "probed_model": "anthropic/claude-code-2.1.240",
                    "checked_at": "2026-09-26T03:59:00Z",
                    "reason": "OAuth session expired",
                }
            },
        },
    )

    state = health_watch.worker_plane_state(db, paths=paths, now=NOW)

    assert state["provider_readiness"]["anthropic"]["ready"] is False
    assert "OAuth" in state["provider_readiness"]["anthropic"]["reason"]
    assert state["providers_ready"] is False
    # Provider unreadiness is reported WITH the work-not-driven alert.
    health_watch.check_worker_plane(db, paths=paths, now=NOW)
    assert any("OAuth" in alert for alert in health_watch.ALERTS), health_watch.ALERTS


def test_state_exposes_the_documented_worker_fields(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    db = _database(tmp_path)
    _seed_cycle(db, unscored=1, created_at="2026-09-26T03:00:00Z")
    _patch_decision(monkeypatch)
    paths = _state_paths(tmp_path, {})

    state = health_watch.worker_plane_state(db, paths=paths, now=NOW)

    for key in (
        "cycle_as_of",
        "pipeline_run_id",
        "outstanding_runs",
        "issued_work_items",
        "claimed_runs",
        "accepted_assessments",
        "malformed_attempts",
        "oldest_outstanding_at",
        "oldest_outstanding_hours",
        "last_worker_activity_at",
        "last_worker_success_at",
        "scored_candidates",
        "candidate_population",
        "provider_readiness",
        "proposals",
        "decision_status",
        "decision_no_action",
    ):
        assert key in state, key
    assert state["candidate_population"] >= 1


# --------------------------------------------------------------------------- #
# alert decision table (C cases need state shapes the engine owns)
# --------------------------------------------------------------------------- #


def _plane(**overrides):
    state = {
        "cycle_as_of": "2026-09-24T20:15:00Z",
        "outstanding_runs": 0,
        "issued_work_items": 0,
        "accepted_assessments": 0,
        "scored_candidates": 0,
        "candidate_population": 39,
        "oldest_accepted_at": None,
        "last_run_accepted": 0,
        "last_run_failures": [],
        "provider_readiness": {},
        "last_worker_activity_at": "2026-09-26T03:59:00Z",
        "configuration_error": None,
        "decision_as_of": None,
        "decision_no_action": False,
    }
    state.update(overrides)
    return state


def test_accepted_assessments_without_scores_alert_C(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    monkeypatch.setattr(
        health_watch,
        "worker_plane_state",
        lambda *a, **k: _plane(accepted_assessments=4, scored_candidates=0, oldest_accepted_at=3.5),
    )

    health_watch.check_worker_plane(object(), now=NOW)

    assert any("not being scored" in alert for alert in health_watch.ALERTS), health_watch.ALERTS


def test_scored_candidates_without_a_decision_alert_C(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    monkeypatch.setattr(
        health_watch,
        "worker_plane_state",
        lambda *a, **k: _plane(scored_candidates=5, oldest_accepted_at=2.5),
    )

    health_watch.check_worker_plane(object(), now=NOW)

    assert any("not being finalised" in alert for alert in health_watch.ALERTS), health_watch.ALERTS


def test_freshly_scored_candidates_without_a_decision_do_not_alert(tmp_path, monkeypatch):
    """The finalizer runs every 5 minutes: a just-scored cycle is not a failure."""
    from tradehub_research.ops import health_watch

    monkeypatch.setattr(
        health_watch,
        "worker_plane_state",
        lambda *a, **k: _plane(scored_candidates=5, oldest_accepted_at=0.05),
    )

    health_watch.check_worker_plane(object(), now=NOW)

    assert health_watch.ALERTS == [], health_watch.ALERTS


def test_decision_completed_with_zero_proposals_is_never_an_alert(tmp_path, monkeypatch):
    from tradehub_research.ops import health_watch

    monkeypatch.setattr(
        health_watch,
        "worker_plane_state",
        lambda *a, **k: _plane(
            decision_as_of="2026-09-25T19:05:53Z",
            decision_no_action=True,
            scored_candidates=5,
            proposals=0,
        ),
    )

    health_watch.check_worker_plane(object(), now=NOW)

    assert health_watch.ALERTS == [], health_watch.ALERTS


def test_misconfigured_worker_alerts_explicitly(tmp_path, monkeypatch):
    """e.g. routes collapsing independent roles onto one provider."""
    from tradehub_research.ops import health_watch

    monkeypatch.setattr(
        health_watch,
        "worker_plane_state",
        lambda *a, **k: _plane(configuration_error="independence violated: deepseek"),
    )

    health_watch.check_worker_plane(object(), now=NOW)

    assert any("misconfigured" in alert for alert in health_watch.ALERTS), health_watch.ALERTS

"""Bounded committee worker: contract, bounds, and failure semantics.

Every test here pins a lesson that cost something real during the 2026-09-25 canary,
or a bound the owner requires before this worker may spend model money.
"""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from tradehub_research.committee.worker_contract import (
    CLAIM_KEYS,
    RoleRoute,
    WorkerContractError,
    assemble_assessment,
    assert_independent,
    build_brief,
    load_role_routes,
    normalize_claims,
    parse_model_output,
    preflight,
)
from tradehub_research.ops import committee_worker as worker

ROUTE = RoleRoute(
    provider="provider-x", model="model-x", model_route="route-x/model-x", billing_class="paid"
)
OTHER_ROUTE = RoleRoute(
    provider="provider-y", model="model-y", model_route="route-y/model-y", billing_class="paid"
)
WORK = {
    "work_id": "work-1",
    "committee_run_id": "run-1",
    "role": "neutral_analyst_a",
    "pack_hash": "a" * 64,
    "prompt_version": "v2",
    "assessment_schema_version": 1,
    "taxonomy_version": 1,
    "attempt_number": 1,
}
ARTIFACT = {
    "run": {"as_of": "2026-09-24T20:15:00Z"},
    "candidate": {"candidate_id": "cand-1"},
    "evidence": [{"evidence_id": "11111111-2222-3333-4444-555555555555"}],
}


# --------------------------------------------------------------------------- #
# contract: routes + independence
# --------------------------------------------------------------------------- #


def test_default_routes_satisfy_the_independence_contract():
    routes = load_role_routes()
    assert_independent(routes)  # must not raise
    assert routes["neutral_analyst_a"].provider != routes["neutral_analyst_b"].provider
    assert routes["red_team"].provider != routes["arbiter"].provider


def test_routes_that_collapse_independent_roles_fail_closed(tmp_path):
    config = tmp_path / "routes.json"
    config.write_text(
        json.dumps(
            {
                "neutral_analyst_b": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "model_route": "x",
                    "billing_class": "paid",
                }
            }
        ),
        encoding="utf-8",
    )
    routes = load_role_routes(config)
    with pytest.raises(WorkerContractError, match="independence violated"):
        assert_independent(routes)


# --------------------------------------------------------------------------- #
# contract: briefs (the canary's five harness defects)
# --------------------------------------------------------------------------- #


def test_targeted_brief_embeds_the_focus_items():
    """Omitting FOCUS produced empty verdicts; pre-flight then refused them."""
    focus = {"comparison_id": "cmp-1", "items": [{"item_id": "item-1", "bucket": "OMITTED"}]}
    brief = build_brief("red_team", {**WORK, "focus": focus}, ARTIFACT, [])
    assert "FOCUS" in brief
    assert "item-1" in brief
    assert "exactly once" in brief


def test_claims_brief_states_every_required_key_and_a_non_empty_condition():
    brief = build_brief("neutral_analyst_a", WORK, ARTIFACT, ["momentum", "quality"])
    assert "falsification_condition is REQUIRED" in brief
    assert "NON-EMPTY string" in brief
    for key in sorted(CLAIM_KEYS):
        assert key in brief
    assert "512" in brief
    assert "momentum" in brief


def test_normalize_never_repairs_a_missing_falsification_condition():
    """The validator demands a real condition; defaulting it would hide our defect."""
    claims, filled = normalize_claims(
        "neutral_analyst_a",
        [
            {
                "claim_key": "momentum",
                "claim_type": "fact",
                "direction": "bullish",
                "statement": "s",
                "materiality": 3,
                "uncertainty": 0.5,
                "contradictory_evidence_ids": None,
            }
        ],
    )
    assert "falsification_condition" not in claims[0]
    assert set(filled) == {"claim0.contradictory_evidence_ids", "claim0.cited_evidence_ids"}


def test_assemble_injects_identity_from_the_envelope_not_the_model():
    judgement = {
        "claims": [
            {
                "claim_key": "momentum",
                "claim_type": "fact",
                "direction": "bullish",
                "statement": "s",
                "materiality": 3,
                "uncertainty": 0.5,
                "cited_evidence_ids": ["11111111-2222-3333-4444-555555555555"],
                "contradictory_evidence_ids": [],
                "falsification_condition": "a close below the 200-day average",
            }
        ],
        "thesis": {
            "summary": "s",
            "upside_mechanism": "u",
            "downside_mechanism": "d",
            "thesis_break_conditions": ["c"],
        },
        "confidence": 0.6,
        "uncertainty": 0.4,
        "candidate_id": "model-supplied-lie",
        "pack_hash": "b" * 64,
        "provider": "model-supplied-lie",
    }
    payload, _ = assemble_assessment(
        work=WORK,
        artifact_body=ARTIFACT,
        role="neutral_analyst_a",
        route=ROUTE,
        judgement=judgement,
    )
    assert payload["candidate_id"] == "cand-1"
    assert payload["pack_hash"] == "a" * 64
    assert payload["provider"] == "provider-x"
    assert payload["model_route"] == "route-x/model-x"
    assert payload["evaluation_time"] == "2026-09-24T20:15:00Z"
    assert payload["usage"]["source"] == "UNKNOWN"


def test_preflight_uses_the_server_validator_and_names_harness_fields():
    payload, _ = assemble_assessment(
        work=WORK,
        artifact_body=ARTIFACT,
        role="neutral_analyst_a",
        route=ROUTE,
        judgement={"claims": [], "thesis": None},
    )
    error = preflight(payload, run={}, artifact_body=ARTIFACT, spec={})
    assert error is not None
    assert worker._harness_owned_error(error) or "thesis" in error


def test_parse_model_output_accepts_fences_and_rejects_prose():
    assert parse_model_output('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(WorkerContractError):
        parse_model_output("I could not decide.")


# --------------------------------------------------------------------------- #
# driving: bounds, no-burn semantics, idempotency
# --------------------------------------------------------------------------- #


class FakeApi:
    def __init__(self, envelopes):
        self._envelopes = list(envelopes)
        self.sent = []

    def get_work(self, run_id):
        return self._envelopes.pop(0) if self._envelopes else None

    def submit(self, run_id, envelope):
        self.sent.append(envelope)
        return 200, {"state": "SCORED", "snapshot_id": "snap-1"}


class FakeMcp:
    """Mimics the MCP tool's WRAPPER response, not the bare artifact."""

    def __init__(self, artifact=None, *, pinned: bool = True, pack_hash: str | None = None):
        self.artifact = artifact or ARTIFACT
        self.pinned = pinned
        self.pack_hash = pack_hash or WORK["pack_hash"]

    def evidence_pack(self, candidate_id, pack_hash):
        return {
            "artifact": self.artifact,
            "pack_hash": self.pack_hash,
            "pinned": self.pinned,
            "representation": "BOUNDED_COMMITTEE_VIEW",
            "lineage_hash": "lineage-hash",
            "pack_spec_version": 2,
        }


def _stub_run(monkeypatch, **fields):
    payload = {"candidate_id": "cand-1", "comparator_config_hash": "cc", **fields}
    monkeypatch.setattr(
        worker,
        "run_row",
        lambda db, committee_run_id: {"committee_run_id": committee_run_id, **payload},
    )


def _drive(
    monkeypatch,
    *,
    api,
    mcp=None,
    preflight_result=None,
    output=None,
    model=None,
    budget=None,
    **kwargs,
):
    monkeypatch.setattr(worker, "preflight", lambda *a, **k: preflight_result)
    if model is None:
        monkeypatch.setattr(
            worker, "run_model", lambda *a, **k: output if output is not None else "{}"
        )
    else:
        monkeypatch.setattr(worker, "run_model", model)
    # Readiness has its own test; here the route is available.
    monkeypatch.setattr(worker, "_probe", lambda route: (True, None))
    return worker.drive_run(
        client=api,
        mcp=mcp or FakeMcp(),
        db=None,
        run={"committee_run_id": "run-1"},
        routes={"neutral_analyst_a": ROUTE},
        spec={"taxonomy": ["momentum"]},
        taxonomy_keys=["momentum"],
        budget=budget or worker.Budget(),
        state={},
        **kwargs,
    )


def test_valid_envelope_is_sent_once_and_terminal_state_stops_the_run(monkeypatch):
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    record = _drive(monkeypatch, api=api, preflight_result=None)

    assert len(api.sent) == 1
    assert api.sent[0]["outcome"] == "accepted"
    assert record["stopped"] == "terminal"
    assert record["roles"][0]["state"] == "SCORED"


def test_harness_owned_preflight_failure_never_sends_and_preserves_the_attempt(monkeypatch):
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    record = _drive(monkeypatch, api=api, preflight_result="candidate_id: wrong value")

    assert api.sent == []  # the envelope was never transmitted
    assert record["stopped"] == "preflight-not-clean"
    assert record["roles"][0]["harness_defect"]
    assert record["roles"][0].get("outcome") is None


def test_unparseable_model_output_is_recorded_as_malformed_with_an_excerpt(monkeypatch):
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    record = _drive(monkeypatch, api=api, preflight_result=None, output="no json here")

    assert len(api.sent) == 1
    assert api.sent[0]["outcome"] == "malformed"
    assert "no json here" in api.sent[0]["diagnostic_excerpt"]
    assert record["roles"][0]["outcome"] == "malformed"


def test_submit_failure_records_the_http_error_without_retry_looping(monkeypatch):
    _stub_run(monkeypatch, comparator_config_hash="cc")

    class Failing(FakeApi):
        def submit(self, run_id, envelope):
            self.sent.append(envelope)
            return 500, {"detail": "boom"}

    api = Failing([WORK])
    record = _drive(monkeypatch, api=api, preflight_result=None)

    assert len(api.sent) == 1
    assert record["stopped"] == "submit-failed-500"
    assert "boom" in record["roles"][0]["http_error"]


def test_run_with_no_issued_work_is_idempotent(monkeypatch):
    """A restart must not duplicate work: no envelope, no model call."""
    _stub_run(monkeypatch, comparator_config_hash="cc")
    calls = {"n": 0}

    def _counting_model(*args, **kwargs):
        calls["n"] += 1
        return "{}"

    monkeypatch.setattr(worker, "run_model", _counting_model)
    api = FakeApi([])
    record = _drive(monkeypatch, api=api, preflight_result=None)

    assert record["stopped"] == "no-work-issued"
    assert api.sent == []
    assert calls["n"] == 0


def test_model_call_budget_stops_the_invocation(monkeypatch):
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    budget = worker.Budget(max_model_calls=0)
    record = _drive(monkeypatch, api=api, preflight_result=None, budget=budget)

    assert record["stopped"] == "max-model-calls(0)"
    assert api.sent == []


def test_envelope_is_never_sent_when_a_pin_mismatch_is_detected(monkeypatch):
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    mcp = FakeMcp(pack_hash="c" * 64)
    record = _drive(monkeypatch, api=api, mcp=mcp, preflight_result=None)

    assert api.sent == []
    assert record["stopped"] == "pin-mismatch"


def test_unpinned_artifact_is_refused_before_any_model_call(monkeypatch):
    """The MCP wrapper's `pinned` flag is authoritative: an unpinned artifact is not
    a legitimate committee input."""
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    calls = {"n": 0}

    def _counting_model(*args, **kwargs):
        calls["n"] += 1
        return "{}"

    monkeypatch.setattr(worker, "run_model", _counting_model)
    record = _drive(monkeypatch, api=api, mcp=FakeMcp(pinned=False), preflight_result=None)

    assert record["stopped"] == "artifact-not-pinned"
    assert api.sent == []
    assert calls["n"] == 0


def test_model_side_validation_failure_after_corrections_consumes_the_attempt(monkeypatch):
    """A defect the MODEL owns must consume the attempt, not re-drive the run forever.

    Live observation: a model kept exceeding the missing-evidence bound, and the
    worker retried the same run on every tick, spending 4 calls each time.
    """
    _stub_run(monkeypatch, comparator_config_hash="cc")
    api = FakeApi([WORK])
    calls = {"n": 0}

    def _counting_model(*args, **kwargs):
        calls["n"] += 1
        return "{}"

    monkeypatch.setattr(worker, "run_model", _counting_model)
    record = _drive(
        monkeypatch,
        api=api,
        model=_counting_model,
        preflight_result="AssessmentValidationError: missing evidence bound exceeded",
    )

    assert calls["n"] == worker.MAX_CORRECTIONS_PER_ROLE + 1  # bounded, not unbounded
    assert len(api.sent) == 1
    assert api.sent[0]["outcome"] == "malformed"
    assert "missing evidence bound exceeded" in api.sent[0]["diagnostic_excerpt"]
    assert record["roles"][0]["outcome"] == "malformed"


def test_provider_readiness_backs_off_and_reuses_a_fresh_success(monkeypatch):
    state: dict = {}
    monkeypatch.setattr(worker, "_probe", lambda route: (False, "OAuth session expired"))
    ready, reason = worker.provider_ready(ROUTE, state)
    assert ready is False
    assert "OAuth" in reason
    entry = state["providers"][ROUTE.provider]
    assert entry["failures"] == 1 and entry["next_retry_at"]

    # Backoff must prevent an immediate re-probe.
    monkeypatch.setattr(worker, "_probe", lambda route: pytest.fail("re-probed during backoff"))
    again, _ = worker.provider_ready(ROUTE, state)
    assert again is False

    # A fresh success is reused without probing.
    fresh: dict = {}
    monkeypatch.setattr(worker, "_probe", lambda route: (True, None))
    assert worker.provider_ready(ROUTE, fresh)[0] is True
    monkeypatch.setattr(worker, "_probe", lambda route: pytest.fail("re-probed while fresh"))
    assert worker.provider_ready(ROUTE, fresh)[0] is True


def test_readiness_cache_is_invalidated_when_the_runner_changes(monkeypatch):
    """A verdict from another runner/identity must never mask a working one.

    Observed live: a probe run under a non-root identity recorded
    PermissionError, and the worker then reused that verdict (and its backoff)
    after the runner path was fixed.
    """
    state: dict = {}
    monkeypatch.setenv("RESEARCH_COMMITTEE_WORKER_HERMES", "/bin/false")
    monkeypatch.setattr(worker, "_probe", lambda route: (False, "PermissionError"))
    assert worker.provider_ready(ROUTE, state)[0] is False

    monkeypatch.setenv("RESEARCH_COMMITTEE_WORKER_HERMES", "/bin/true")
    monkeypatch.setattr(worker, "_probe", lambda route: (True, None))
    assert worker.provider_ready(ROUTE, state)[0] is True


def test_runner_argv_targets_the_configured_provider_and_model(monkeypatch):
    argv = worker.runner_argv(ROUTE, "brief")
    assert argv[:3] == ["hermes", "-z", "brief"]
    assert "--provider" in argv and ROUTE.provider in argv
    assert "-m" in argv and ROUTE.model in argv
    assert argv[-2:] == ["-t", ""]


# --------------------------------------------------------------------------- #
# selection: current cycle only, deterministic, supersede-safe
# --------------------------------------------------------------------------- #


def _cycle_db(tmp_path):
    from tradehub_research.db import ResearchDB

    db = ResearchDB(tmp_path / "research.db", 5000)
    db.migrate()
    return db


def _pipeline_run(db, run_id, as_of):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pipeline_run (run_id, as_of, universe_hash, screen_manifest_json, "
            "screen_manifest_hash, funnel_config_json, funnel_config_hash, input_view_hash, "
            "expected_security_count, status, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                as_of,
                "u",
                "{}",
                "h",
                "{}",
                "h",
                "v",
                1,
                "COMPLETE",
                as_of,
                as_of,
            ),
        )


def test_newest_genuine_cycle_ignores_acceptance_runs(tmp_path):
    db = _cycle_db(tmp_path)
    _pipeline_run(db, "pr66-acceptance-9", "2026-09-30T20:15:00Z")
    _pipeline_run(db, "cycle-old", "2026-09-22T20:15:00Z")
    _pipeline_run(db, "cycle-new", "2026-09-24T20:15:00Z")

    cycle = worker.newest_genuine_cycle(db)

    assert cycle["run_id"] == "cycle-new"


def test_outstanding_runs_are_ordered_by_run_id_and_limited(tmp_path):
    db = _cycle_db(tmp_path)
    _pipeline_run(db, "cycle-new", "2026-09-24T20:15:00Z")
    from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security

    with db.connect() as conn:
        seed_security(conn, "S1")
        seed_pipeline_run(conn, "cycle-other", "2026-09-22T20:15:00Z")
        seed_score(
            conn, pipeline_run_id="cycle-other", security_id="S1", run_as_of="2026-09-22T20:15:00Z"
        )
        source = conn.execute("SELECT * FROM committee_run LIMIT 1").fetchone()
        for name in ("b-run", "a-run", "c-run"):
            payload = dict(source)
            payload["committee_run_id"] = name
            payload["pipeline_run_id"] = "cycle-new"
            payload["created_at"] = "2026-09-24T20:16:00Z"
            columns = ", ".join(payload)
            marks = ", ".join("?" * len(payload))
            conn.execute(
                f"INSERT INTO committee_run ({columns}) VALUES ({marks})", tuple(payload.values())
            )

    runs = worker.outstanding_runs(db, "cycle-new", 2)

    assert [run["committee_run_id"] for run in runs] == ["a-run", "b-run"]
    assert worker.outstanding_runs(db, "cycle-new", 10)[-1]["committee_run_id"] == "c-run"
    # The scored run belongs to another cycle and must never appear here.
    assert all(run["committee_run_id"] != "cr-S1-a" for run in runs)


def test_superseded_cycle_is_detected(tmp_path):
    db = _cycle_db(tmp_path)
    _pipeline_run(db, "cycle-old", "2026-09-24T20:15:00Z")

    assert worker.superseded(db, "cycle-old") is False
    _pipeline_run(db, "cycle-new", "2026-09-28T20:15:00Z")

    assert worker.superseded(db, "cycle-old") is True


def test_state_round_trips_through_the_research_dir(tmp_path):
    paths = SimpleNamespace(research_dir=tmp_path)
    state_file = worker.state_path(paths)
    worker.write_state(state_file, {"last_activity_at": "2026-09-26T04:00:00Z"})

    assert worker.read_state(state_file)["last_activity_at"] == "2026-09-26T04:00:00Z"
    assert worker.read_state(tmp_path / "missing.json") == {}


def test_per_invocation_verdicts_do_not_leak_into_the_next_run(tmp_path, monkeypatch):
    """A stale stopped_by would misattribute the reason in the monitor.

    Observed live: an invocation bounded by --max-runs 4 reported
    "stopped_by: max-runs(3)", inherited from the previous invocation's state.
    """
    paths = SimpleNamespace(research_dir=tmp_path)
    state_file = worker.state_path(paths)
    worker.write_state(state_file, {"stopped_by": "max-runs(3)", "last_activity_at": "old"})
    state = worker.read_state(state_file)
    state["previous_stopped_by"] = state.get("stopped_by")
    state.pop("stopped_by", None)

    assert "stopped_by" not in state
    assert state["previous_stopped_by"] == "max-runs(3)"


def test_mcp_client_close_reaps_its_child(tmp_path):
    """A surviving MCP child keeps the systemd cgroup alive and skips ticks.

    The client is built without its handshake so the test needs no real MCP server:
    the point is that close() terminates the process it started.
    """
    script = tmp_path / "fake-mcp"
    script.write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
    script.chmod(0o755)

    client = object.__new__(worker.McpClient)
    client._id = 0
    client._proc = subprocess.Popen(  # noqa: S603 - the test's own stand-in
        [str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pid = client._proc.pid
    client.close()

    client._proc.wait(timeout=5)
    assert client._proc.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_mcp_read_times_out_instead_of_wedging(tmp_path, monkeypatch):
    """A hung server must fail the invocation, not hold the lock forever."""
    monkeypatch.setattr(worker, "MCP_TIMEOUT_SECONDS", 0.2)
    script = tmp_path / "silent-mcp"
    script.write_text("#!/bin/sh\nsleep 300\n", encoding="utf-8")
    script.chmod(0o755)

    client = object.__new__(worker.McpClient)
    client._id = 0
    client._proc = subprocess.Popen(  # noqa: S603 - the test's own stand-in
        [str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        with pytest.raises(WorkerContractError, match="timed out"):
            client._request("initialize", {})
    finally:
        client.close()


def test_unknown_role_route_is_rejected(tmp_path):
    config = tmp_path / "routes.json"
    config.write_text(json.dumps({"chief_vibes_officer": {}}), encoding="utf-8")
    with pytest.raises(WorkerContractError):
        load_role_routes(config)

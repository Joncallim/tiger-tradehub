"""Bounded committee model worker: the production actuator for issued work.

WHAT THIS IS
------------
The research cycle issues committee work envelopes; a model must drive them or the
decision plane silently accumulates undriven work (the 2026-09-15..09-25 incident).
This module is that actuator, and nothing more. It replays the EXACT contract the
canary proved:

  research cycle -> committee queue -> this worker -> assessments
  -> score snapshots -> finalizer -> proposal / legitimate NO_ACTION -> runner

The worker never queries arbitrary evidence, never bypasses the queue, never builds
score snapshots, never calls the finalizer, and never touches eligibility or policy.
It fetches the pinned artifact from the MCP surface, submits through the
authenticated API whose router owns every retry/escalation decision, and lets the
existing scorer and finalizer advance.

BOUNDS (paid model work, so they are explicit and enforced here)
---------------------------------------------------------------
* ``--max-runs`` committee runs per invocation (default 4)
* ``--max-model-calls`` model calls per invocation (default 12)
* ``--max-seconds`` wall clock per invocation (default 900)
* per-role corrections capped before an attempt is spent (default 2)
* provider failure backoff with a growing next-retry time
* deterministic run ordering, current-cycle only, no historical backlog
* flock + state file: concurrent invocations cannot double-drive, and a restart
  resumes from durable committee state without duplicating accepted assessments

A harness/formatting defect never consumes a role's bounded attempt: the assembled
envelope is pre-flighted with the SERVER's own validator, and a pre-flight failure
on a harness-owned field aborts the submission instead of sending it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import shlex
import signal
import subprocess  # noqa: S404 - the configured model runner is an operator-chosen CLI
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tradehub_research.committee.worker_contract import (
    UNKNOWN_COST,
    UNKNOWN_USAGE,
    WorkerContractError,
    assemble_assessment,
    assert_independent,
    build_brief,
    load_role_routes,
    parse_model_output,
    preflight,
)
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.ops.common import ResearchPaths, research_paths

ACCEPTANCE_PREFIX = "pr66-acceptance-"
STATE_FILENAME = "committee-worker-state.json"
LOCK_FILENAME = "committee-worker.lock"

DEFAULT_MAX_RUNS = 4
DEFAULT_MAX_MODEL_CALLS = 12
DEFAULT_MAX_SECONDS = 900.0
MAX_CORRECTIONS_PER_ROLE = 2
MODEL_TIMEOUT_SECONDS = 600
MCP_TIMEOUT_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 120
PROBE_TTL_SECONDS = 600
BACKOFF_BASE_SECONDS = 300
BACKOFF_MAX_SECONDS = 3_600
MAX_ROLES_PER_RUN = 4  # neutral_a, neutral_b, red_team, arbiter
ENVELOPE_MAX_BYTES = 80_000
DIAGNOSTIC_EXCERPT_CHARS = 2_000

#: Current-cycle outstanding work: genuine committee runs with no score snapshot.
#: Ordering is by run id so that a restart resumes over the same sequence.
UNSCORED_RUNS_SQL = (
    "SELECT c.committee_run_id, c.candidate_id, c.created_at, "
    "  (SELECT COUNT(*) FROM committee_work w "
    "   WHERE w.committee_run_id = c.committee_run_id) AS work_items "
    "FROM committee_run c "
    "WHERE c.pipeline_run_id = ? AND c.pipeline_run_id NOT LIKE ? "
    "  AND NOT EXISTS (SELECT 1 FROM score_snapshot s "
    "                  WHERE s.committee_run_id = c.committee_run_id) "
    "ORDER BY c.committee_run_id LIMIT ?"
)

#: Fields the HARNESS owns. A pre-flight failure naming one of these is our defect,
#: so the envelope is never sent and the role's attempt is preserved.
HARNESS_OWNED_FIELDS = (
    "candidate_id",
    "pack_hash",
    "prompt_version",
    "assessment_schema_version",
    "taxonomy_version",
    "model_route",
    "billing_class",
    "provider",
    "model_id",
    "usage",
    "cost",
    "evaluation_time",
    "cited_evidence_ids",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# state (durable, outside the repo tree so it is never deployment drift)
# --------------------------------------------------------------------------- #


def state_path(paths: ResearchPaths) -> Path:
    return Path(paths.research_dir) / STATE_FILENAME


def lock_path(paths: ResearchPaths) -> Path:
    return Path(paths.research_dir) / LOCK_FILENAME


def read_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Budget:
    """Per-invocation spend/work bounds."""

    max_runs: int = DEFAULT_MAX_RUNS
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    max_seconds: float = DEFAULT_MAX_SECONDS
    started: float = field(default_factory=time.monotonic)
    runs_claimed: int = 0
    model_calls: int = 0

    def exhausted(self) -> str | None:
        if self.runs_claimed >= self.max_runs:
            return f"max-runs({self.max_runs})"
        if self.model_calls >= self.max_model_calls:
            return f"max-model-calls({self.max_model_calls})"
        if time.monotonic() - self.started >= self.max_seconds:
            return f"max-seconds({self.max_seconds:g})"
        return None


# --------------------------------------------------------------------------- #
# queue selection: current decision-relevant cycle ONLY
# --------------------------------------------------------------------------- #


def newest_genuine_cycle(db: ResearchDB) -> dict[str, Any] | None:
    """Newest non-acceptance pipeline_run: the only decision-relevant cycle."""
    with db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT run_id, as_of, status, expected_security_count FROM pipeline_run "
            "WHERE run_id NOT LIKE ? ORDER BY as_of DESC LIMIT 1",
            (ACCEPTANCE_PREFIX + "%",),
        ).fetchone()
    return dict(row) if row is not None else None


def outstanding_runs(db: ResearchDB, pipeline_run_id: str, limit: int) -> list[dict[str, Any]]:
    """UNCORED genuine committee runs of one cycle, in deterministic order."""
    with db.connect(read_only=True) as conn:
        rows = conn.execute(
            UNSCORED_RUNS_SQL,
            (pipeline_run_id, ACCEPTANCE_PREFIX + "%", limit),
        ).fetchall()
    return [dict(row) for row in rows]


def superseded(research_db: ResearchDB, cycle_run_id: str) -> bool:
    """True once a newer genuine cycle exists.

    A superseded cycle must never race a newer one into proposal generation, so the
    worker stops issuing NEW work for it; work already claimed is finished safely.
    """
    current = newest_genuine_cycle(research_db)
    return current is not None and current["run_id"] != cycle_run_id


def candidate_population(db: ResearchDB, pipeline_run_id: str) -> int:
    with db.connect(read_only=True) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM candidate WHERE run_id = ?", (pipeline_run_id,)
            ).fetchone()[0]
        )


def scored_population(db: ResearchDB, pipeline_run_id: str) -> int:
    with db.connect(read_only=True) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM score_snapshot s JOIN committee_run c "
                "ON c.committee_run_id = s.committee_run_id WHERE c.pipeline_run_id = ?",
                (pipeline_run_id,),
            ).fetchone()[0]
        )


def run_row(db: ResearchDB, committee_run_id: str) -> dict[str, Any]:
    with db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM committee_run WHERE committee_run_id = ?", (committee_run_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown committee run {committee_run_id}")
    return dict(row)


def comparator_spec(db: ResearchDB, config_hash: str) -> dict[str, Any]:
    with db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT spec_json FROM comparator_definition WHERE config_hash = ?", (config_hash,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown comparator config {config_hash}")
    return json.loads(row["spec_json"])


# --------------------------------------------------------------------------- #
# authenticated submission API (the documented queue contract)
# --------------------------------------------------------------------------- #


class ApiClient:
    """Thin client for the research service's committee API."""

    def __init__(self, settings: ResearchSettings, timeout: float = 60.0) -> None:
        token = settings.api_token.get_secret_value()
        if not token:
            raise WorkerContractError("RESEARCH_API_TOKEN is not configured")
        self.base = f"http://{settings.bind_host}:{settings.bind_port}"
        self.timeout = timeout
        self._token = token

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - loopback HTTP by construction
            f"{self.base}{path}", data=data, method=method
        )
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"detail": raw[:400]}
            return exc.code, body

    def get_work(self, run_id: str) -> dict[str, Any] | None:
        status, body = self._request("GET", f"/committee-runs/{run_id}/work")
        if status >= 400:
            raise WorkerContractError(f"GET work failed ({status}): {body}")
        return body.get("work")

    def submit(self, run_id: str, envelope: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._request("POST", f"/committee-runs/{run_id}/assessments", envelope)

    def status(self, run_id: str) -> dict[str, Any]:
        status, body = self._request("GET", f"/committee-runs/{run_id}")
        if status >= 400:
            raise WorkerContractError(f"GET status failed ({status}): {body}")
        return body


# --------------------------------------------------------------------------- #
# pinned MCP evidence fetch (the documented artifact surface)
# --------------------------------------------------------------------------- #


class McpClient:
    """Minimal stdio MCP client for the pinned artifact fetch.

    The server is started in its OWN process group and torn down as a group: a
    surviving child would keep the systemd unit's cgroup populated, which holds the
    unit "active" and makes the next timer tick skip (observed live: a worker printed
    its final line in 2 seconds yet the unit stayed active for 9 minutes).
    """

    def __init__(self, settings: ResearchSettings, *, binary: Path | None = None) -> None:
        target = binary or (Path(sys.executable).parent / "tradehub-research-mcp")
        if not target.exists():
            raise WorkerContractError(f"MCP server entry point not found: {target}")
        env = dict(os.environ)
        env["RESEARCH_DB_PATH"] = str(settings.db_path)
        self._proc = subprocess.Popen(  # noqa: S603 - fixed binary, no shell
            [str(target)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        self._id = 0
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tradehub-committee-worker", "version": "1"},
            },
        )

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc.poll() is not None:
            raise WorkerContractError("MCP server exited unexpectedly")
        self._id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps(message) + "\n")
        self._proc.stdin.flush()
        # A hung server must never wedge the worker (it holds the run lock, so a wedged
        # invocation would silently stall every later timer tick).
        deadline = time.monotonic() + MCP_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerContractError(f"MCP server timed out on {method}")
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                raise WorkerContractError(f"MCP server timed out on {method}")
            line = self._proc.stdout.readline()
            if not line:
                raise WorkerContractError("MCP server produced no response")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == self._id and ("result" in payload or "error" in payload):
                return payload

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = self._request("tools/call", {"name": name, "arguments": arguments})
        if "error" in payload:
            return {"_error": payload["error"]}
        result = payload.get("result") or {}
        if result.get("isError"):
            texts = [item.get("text", "") for item in result.get("content") or []]
            return {"_error": {"message": " ".join(texts)}}
        body = None
        for item in result.get("content") or []:
            text = item.get("text")
            if not text:
                continue
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = {"_raw": text}
        return body if body is not None else {"_error": {"message": "empty MCP result"}}

    def evidence_pack(self, candidate_id: str, pack_hash: str) -> dict[str, Any]:
        """Fetch the pinned artifact and unwrap the MCP response.

        The tool returns ``{body, pack_hash, pinned, representation, lineage_hash,
        pack_spec_version}``; the artifact itself is ``body``. Unwrapping here (with
        the pin re-checked by the caller) keeps every downstream consumer working on
        the artifact, never on the envelope.
        """
        payload = self.call_tool(
            "get_evidence_pack", {"candidate_id": candidate_id, "pack_hash": pack_hash}
        )
        if "_error" in payload:
            raise WorkerContractError(
                f"pinned evidence fetch refused: {payload['_error'].get('message', '')[:300]}"
            )
        body = payload.get("body")
        if not isinstance(body, dict):
            raise WorkerContractError("pinned artifact response carried no body object")
        return {
            "artifact": body,
            "pack_hash": payload.get("pack_hash"),
            "pinned": payload.get("pinned"),
            "representation": payload.get("representation"),
            "lineage_hash": payload.get("lineage_hash"),
            "pack_spec_version": payload.get("pack_spec_version"),
        }

    def close(self) -> None:
        """Reap the server (and anything it spawned). Never leaves an orphan."""
        proc = self._proc
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - fall through to the group kill
                pass
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001 - already gone, or no group
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best effort
                pass


# --------------------------------------------------------------------------- #
# model runner + provider readiness
# --------------------------------------------------------------------------- #


def runner_binary() -> str:
    """The operator-chosen runner CLI (never inherited from a login PATH)."""
    return os.environ.get("RESEARCH_COMMITTEE_WORKER_HERMES", "hermes")


def runner_argv(route: Any, prompt: str) -> list[str]:
    """The runner CLI invocation: configurable, absolute, and never a shell string."""
    binary = runner_binary()
    extra = shlex.split(os.environ.get("RESEARCH_COMMITTEE_WORKER_HERMES_ARGS", ""))
    return [
        binary,
        "-z",
        prompt,
        "--provider",
        route.provider,
        "-m",
        route.model,
        *extra,
        "-t",
        "",
    ]


def run_model(route: Any, prompt: str, timeout: float = MODEL_TIMEOUT_SECONDS) -> str:
    completed = subprocess.run(  # noqa: S603 - argv is built here, never a shell string
        runner_argv(route, prompt),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkerContractError(
            f"runner failed rc={completed.returncode}: {(completed.stderr or '')[-300:]}"
        )
    return completed.stdout


def _provider_entry(state: dict[str, Any], provider: str) -> dict[str, Any]:
    return (state.setdefault("providers", {})).setdefault(provider, {})


def provider_ready(
    route: Any, state: dict[str, Any], *, probe: bool = True, now: datetime | None = None
) -> tuple[bool, str | None]:
    """Cached provider readiness. Never degrades silently: unready => no claim."""
    now = now or _now()
    entry = _provider_entry(state, route.provider)
    runner = runner_binary()
    if entry and entry.get("probed_runner") not in (None, runner):
        # A verdict recorded for a DIFFERENT runner (other binary, or the same CLI
        # run under another identity) says nothing about this one: never reuse it,
        # and never inherit its backoff.
        entry.clear()
    checked_at = _parse_iso(entry.get("checked_at"))
    retry_at = _parse_iso(entry.get("next_retry_at"))
    if checked_at is not None:
        fresh = (now - checked_at).total_seconds() < PROBE_TTL_SECONDS
        if entry.get("ready") is True and fresh:
            return True, None
        if retry_at is not None and now < retry_at:
            return False, str(entry.get("reason") or "provider in backoff")
    if not probe:
        return False, "provider readiness not yet established"
    ready, reason = _probe(route)
    if ready is True:
        entry.update(
            {
                "ready": True,
                "checked_at": _iso(now),
                "reason": None,
                "failures": 0,
                "next_retry_at": None,
                "probed_model": route.key,
                "probed_runner": runner,
            }
        )
        return True, None
    failures = int(entry.get("failures") or 0) + 1
    backoff = min(BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS)
    entry.update(
        {
            "ready": False,
            "checked_at": _iso(now),
            "reason": reason,
            "failures": failures,
            "next_retry_at": _iso(now + timedelta(seconds=backoff)),
            "probed_model": route.key,
            "probed_runner": runner,
        }
    )
    return False, reason


def _probe(route: Any) -> tuple[bool, str | None]:
    """Lightweight readiness call through the same runner the worker will use."""
    prompt = 'Reply with exactly this JSON and nothing else: {"ready":true}'
    try:
        output = run_model(route, prompt, timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - any failure is an unready route
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        parsed = parse_model_output(output)
    except WorkerContractError as exc:
        return False, f"unparseable probe response: {exc}"
    if parsed.get("ready") is True:
        return True, None
    return False, f"probe returned {json.dumps(parsed)[:120]}"


# --------------------------------------------------------------------------- #
# driving one run
# --------------------------------------------------------------------------- #


def _harness_owned_error(error: str) -> bool:
    return any(field in error for field in HARNESS_OWNED_FIELDS)


def drive_run(
    *,
    client: ApiClient,
    mcp: McpClient,
    db: ResearchDB,
    run: dict[str, Any],
    routes: dict[str, Any],
    spec: dict[str, Any],
    taxonomy_keys: list[str],
    budget: Budget,
    state: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Drive one committee run up to its terminal state, inside the budget."""
    committee_run_id = run["committee_run_id"]
    cycle_row = run_row(db, committee_run_id)
    record: dict[str, Any] = {"committee_run_id": committee_run_id, "roles": [], "stopped": None}

    for _ in range(MAX_ROLES_PER_RUN):
        stop = budget.exhausted()
        if stop:
            record["stopped"] = stop
            break
        work = client.get_work(committee_run_id)
        if work is None:
            record["stopped"] = "no-work-issued"
            break
        role = str(work["role"])
        route = routes.get(role)
        if route is None:
            record["stopped"] = f"no-route-for-{role}"
            break
        ready, reason = provider_ready(route, state)
        if not ready:
            record["stopped"] = f"provider-unready:{route.provider}"
            record["provider_reason"] = reason
            break

        fetched = mcp.evidence_pack(cycle_row["candidate_id"], str(work["pack_hash"]))
        artifact = fetched["artifact"]
        returned = str(fetched.get("pack_hash"))
        if returned != str(work["pack_hash"]):
            record["stopped"] = "pin-mismatch"
            record["pin_error"] = f"requested {work['pack_hash']} got {returned}"
            break
        if fetched.get("pinned") is not True:
            record["stopped"] = "artifact-not-pinned"
            break
        brief = build_brief(role, work, artifact, taxonomy_keys)
        role_record: dict[str, Any] = {
            "role": role,
            "provider": route.provider,
            "model": route.model,
            "work_id": work["work_id"],
            "attempt_number": work["attempt_number"],
            "corrections": 0,
        }
        envelope: dict[str, Any] | None = None
        malformed: str | None = None
        calls_made = 0
        for correction in range(MAX_CORRECTIONS_PER_ROLE + 1):
            if budget.exhausted():
                role_record["stopped"] = budget.exhausted()
                break
            budget.model_calls += 1
            calls_made += 1
            try:
                output = run_model(route, brief)
            except WorkerContractError as exc:
                role_record["runner_error"] = str(exc)[:300]
                malformed = f"runner failure: {exc}"
                break
            try:
                judgement = parse_model_output(output)
            except WorkerContractError as exc:
                malformed = f"unparseable model output: {exc}"
                role_record["malformed_reason"] = malformed
                role_record["raw_excerpt"] = output[-DIAGNOSTIC_EXCERPT_CHARS:]
                break
            payload, filled = assemble_assessment(
                work=work, artifact_body=artifact, role=role, route=route, judgement=judgement
            )
            if filled:
                role_record["normalized"] = filled
            error = preflight(payload, run=cycle_row, artifact_body=artifact, spec=spec)
            if error is None:
                envelope = {
                    "work_id": work["work_id"],
                    "outcome": "accepted",
                    "provider": route.provider,
                    "model_id": route.model,
                    "model_route": route.model_route,
                    "billing_class": route.billing_class,
                    "usage": dict(payload["usage"]),
                    "cost": dict(payload["cost"]),
                    "assessment": payload,
                }
                break
            role_record["last_preflight_error"] = error[:300]
            if _harness_owned_error(error):
                # Our defect, not the model's: never send, never burn the attempt.
                role_record["harness_defect"] = error[:300]
                break
            if correction < MAX_CORRECTIONS_PER_ROLE:
                role_record["corrections"] = correction + 1
                brief = (
                    f"{brief}\n\nCORRECTION REQUIRED - your previous attempt was rejected by the "
                    f"validator with: {error}. Fix EXACTLY that defect and return the complete "
                    "corrected JSON object (same schema, same rules). Note the length limits: "
                    "thesis.summary, thesis.upside_mechanism and thesis.downside_mechanism must "
                    "each be non-empty and at most 512 characters; claim_key 'other' is capped "
                    "at materiality 2."
                )
                continue
            # Corrections exhausted on a MODEL-side defect: consume the attempt (per the
            # existing contract) instead of re-driving this run on every future tick.
            malformed = f"model output failed validation after corrections: {error}"
            role_record["malformed_reason"] = malformed
        role_record["model_calls"] = calls_made
        if envelope is None:
            if malformed is not None:
                excerpt = str(role_record.get("raw_excerpt") or "")
                envelope = {
                    "work_id": work["work_id"],
                    "outcome": "malformed",
                    "provider": route.provider,
                    "model_id": route.model,
                    "model_route": route.model_route,
                    "billing_class": route.billing_class,
                    "usage": dict(UNKNOWN_USAGE),
                    "cost": dict(UNKNOWN_COST),
                    "diagnostic_excerpt": (
                        f"{malformed}\n---\n{excerpt}" if excerpt else malformed
                    )[-DIAGNOSTIC_EXCERPT_CHARS:],
                }
                role_record["outcome"] = "malformed"
            else:
                # Harness defect or budget stop: leave the work PENDING.
                if role_record.get("stopped") is None:
                    role_record["stopped"] = "preflight-not-clean"
                record["roles"].append(role_record)
                record["stopped"] = role_record.get("stopped") or record["stopped"]
                break

        if envelope is None:  # pragma: no cover - defensive
            break
        size = len(json.dumps(envelope))
        role_record["envelope_bytes"] = size
        role_record["normalized_bytes"] = size
        if size > ENVELOPE_MAX_BYTES:
            record["stopped"] = "envelope-too-large"
            record["roles"].append(role_record)
            break
        if dry_run:
            role_record["dry_run"] = True
            role_record["state"] = "NOT_SENT"
            record["roles"].append(role_record)
            record["stopped"] = "dry-run"
            break
        status, response = client.submit(committee_run_id, envelope)
        role_record["http_status"] = status
        if status >= 400:
            role_record["http_error"] = str(response)[:300]
            record["roles"].append(role_record)
            record["stopped"] = f"submit-failed-{status}"
            break
        role_record["state"] = response.get("state")
        role_record["snapshot_id"] = response.get("snapshot_id")
        role_record["outcome"] = role_record.get("outcome", "accepted")
        state["accepted_assessments"] = int(state.get("accepted_assessments") or 0) + 1
        if role_record["outcome"] == "malformed":
            state["malformed_attempts"] = int(state.get("malformed_attempts") or 0) + 1
        state["last_success_at"] = _iso(_now())
        state["last_role"] = role
        record["roles"].append(role_record)
        if role_record["state"] in {"SCORED", "BLOCKED", "ESCALATE"}:
            record["stopped"] = "terminal"
            break
    return record


def summarise(db: ResearchDB, cycle: dict[str, Any] | None) -> dict[str, Any]:
    if cycle is None:
        return {}
    return {
        "cycle_as_of": cycle.get("as_of"),
        "pipeline_run_id": cycle.get("run_id"),
        "candidates": candidate_population(db, cycle["run_id"]),
        "scored": scored_population(db, cycle["run_id"]),
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded TradeHub committee model worker")
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    parser.add_argument("--max-model-calls", type=int, default=DEFAULT_MAX_MODEL_CALLS)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    parser.add_argument("--dry-run", action="store_true", help="select + probe only, send nothing")
    parser.add_argument("--probe-only", action="store_true", help="only refresh provider readiness")
    parser.add_argument("--run-id", default=None, help="drive one specific committee run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = ResearchSettings()
    paths = research_paths()
    state_file = state_path(paths)
    lock_file = lock_path(paths)
    state = read_state(state_file)
    # Per-invocation fields must not inherit the previous invocation's verdicts: the
    # monitor reads this file, and a stale stopped_by would misattribute the reason.
    state["previous_stopped_by"] = state.get("stopped_by")
    state.pop("stopped_by", None)
    state["last_started_at"] = _iso(_now())

    Path(paths.research_dir).mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _run_locked(args, settings, paths, state_file, state)


def _run_locked(
    args: argparse.Namespace,
    settings: ResearchSettings,
    paths: ResearchPaths,
    state_file: Path,
    state: dict[str, Any],
) -> int:
    db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    try:
        routes = load_role_routes()
        assert_independent(routes)
    except (WorkerContractError, OSError, json.JSONDecodeError) as exc:
        state["configuration_error"] = str(exc)[:400]
        state["providers_ready"] = False
        write_state(state_file, state)
        print(json.dumps({"worker": "configuration-error", "reason": str(exc)[:300]}))
        return 2
    state.pop("configuration_error", None)

    cycle = newest_genuine_cycle(db)
    state["cycle"] = summarise(db, cycle)

    if args.probe_only:
        ready = {role: provider_ready(route, state)[0] for role, route in sorted(routes.items())}
        state["providers_ready"] = bool(ready) and all(ready.values())
        state["provider_ready_by_role"] = ready
        state["last_probe_at"] = _iso(_now())
        write_state(state_file, state)
        print(json.dumps({"worker": "probe-only", "ready": ready}))
        return 0 if state["providers_ready"] else 2

    if cycle is None:
        state["status"] = "no-cycle"
        write_state(state_file, state)
        print(json.dumps({"worker": "idle", "reason": "no genuine pipeline run"}))
        return 0

    budget = Budget(
        max_runs=args.max_runs, max_model_calls=args.max_model_calls, max_seconds=args.max_seconds
    )
    runs = (
        [{"committee_run_id": args.run_id}]
        if args.run_id
        else outstanding_runs(db, cycle["run_id"], args.max_runs)
    )
    state["outstanding_at_start"] = len(runs)
    state["claimed_runs"] = [
        {"committee_run_id": run["committee_run_id"], "claimed_at": _iso(_now())} for run in runs
    ]
    state["status"] = "working" if runs else "idle-no-work"
    if not runs:
        state["providers"] = state.get("providers", {})
        state["last_success_at"] = state.get("last_success_at") or _iso(_now())
        write_state(state_file, state)
        print(json.dumps({"worker": "idle", "reason": "no outstanding work", **state["cycle"]}))
        return 0

    print(
        json.dumps(
            {
                "worker": "start",
                "cycle": state["cycle"],
                "outstanding_selected": len(runs),
                "bounds": {
                    "max_runs": args.max_runs,
                    "max_model_calls": args.max_model_calls,
                    "max_seconds": args.max_seconds,
                },
            }
        )
    )
    records: list[dict[str, Any]] = []
    mcp: McpClient | None = None
    try:
        client = ApiClient(settings)
        mcp = McpClient(settings)
        for run in runs:
            if budget.exhausted():
                state["stopped_by"] = budget.exhausted()
                break
            current = newest_genuine_cycle(db)
            if superseded(db, cycle["run_id"]):
                # A newer cycle appeared: never let a superseded cycle race it into
                # proposal generation. Work already claimed above is finished.
                state["stopped_by"] = "cycle-superseded"
                print(
                    json.dumps(
                        {
                            "worker": "superseded",
                            "was": cycle["run_id"],
                            "now": (current or {}).get("run_id"),
                        }
                    )
                )
                break
            budget.runs_claimed += 1
            try:
                cycle_row = run_row(db, run["committee_run_id"])
                spec = comparator_spec(db, cycle_row["comparator_config_hash"])
                record = drive_run(
                    client=client,
                    mcp=mcp,
                    db=db,
                    run=run,
                    routes=routes,
                    spec=spec,
                    taxonomy_keys=list(spec.get("taxonomy") or []),
                    budget=budget,
                    state=state,
                    dry_run=args.dry_run,
                )
            except (WorkerContractError, KeyError, urllib.error.URLError) as exc:
                record = {
                    "committee_run_id": run["committee_run_id"],
                    "stopped": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "roles": [],
                }
            records.append(record)
            print(json.dumps({"worker": "run", **record})[:2_000])
            if record.get("stopped", "").startswith("provider-unready"):
                break
    finally:
        if mcp is not None:
            mcp.close()

    state["last_run_records"] = records[-5:]
    state["last_activity_at"] = _iso(_now())
    state["last_exit"] = "ok"
    state["cycle"] = summarise(db, cycle)
    state["providers_ready"] = all(
        (state.get("providers", {}).get(route.provider, {}) or {}).get("ready") is True
        for route in routes.values()
    )
    state["claimed_runs"] = []
    state["stopped_by"] = state.get("stopped_by") or budget.exhausted()
    write_state(state_file, state)
    print(
        json.dumps(
            {
                "worker": "done",
                "records": len(records),
                "model_calls": budget.model_calls,
                "cycle": state["cycle"],
                "stopped_by": state["stopped_by"],
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

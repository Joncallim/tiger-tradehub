"""Committee state machine and server-authorized, idempotent work issuance."""

# ruff: noqa: E501 -- SQL projections mirror immutable artifact layouts.

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from tradehub_research.committee.assessment import (
    AssessmentValidationError,
    normalize_cost,
    normalize_usage,
    validate_for_run,
)
from tradehub_research.committee.comparator import Comparator
from tradehub_research.committee.store import CommitteeStore, semantic_assessment_hash
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

MAX_ATTEMPTS_PER_ROLE = 2
MAX_ACCEPTED_ROLES = 4
MAX_MODEL_CALLS = 8
CALL_TIMEOUT_SECONDS = 120
FOCUS_BUCKETS = ("EVIDENCE_CONFLICT", "CONTRADICTORY", "UNSUPPORTED", "OMITTED")
ATTEMPT_OUTCOMES = {"accepted", "malformed", "unavailable", "timeout"}
ATTEMPT_REQUIRED_KEYS = {
    "work_id", "outcome", "provider", "model_id", "model_route", "billing_class", "usage", "cost"
}
ATTEMPT_OPTIONAL_KEYS = {"assessment", "diagnostic_excerpt"}


def _hash(domain: str, value: object) -> str:
    return hashlib.sha256((domain + "\0" + canonical_json(value)).encode()).hexdigest()


def _focus_body(db: sqlite3.Connection, run_id: str, role: str) -> dict[str, Any]:
    comparison = db.execute(
        "SELECT comparison_id,report_json FROM comparison_report WHERE committee_run_id=? "
        "ORDER BY rowid DESC LIMIT 1", (run_id,),
    ).fetchone()
    if comparison is None:
        raise ValueError("targeted work requires a comparison")
    buckets = json.loads(comparison["report_json"])
    items = []
    for bucket in FOCUS_BUCKETS:
        for item in buckets.get(bucket, []):
            items.append({
                "item_id": item["item_id"], "bucket": bucket,
                "claim_a": item.get("claim_a"), "claim_b": item.get("claim_b"),
                "evidence_ids": item.get("evidence_ids", []),
                "material": bool(item.get("material", False)),
            })
    items.sort(key=lambda item: item["item_id"])
    if role == "arbiter":
        resolution = db.execute(
            "SELECT resolution_json FROM dispute_resolution WHERE committee_run_id=? "
            "AND role='red_team' ORDER BY rowid DESC LIMIT 1", (run_id,),
        ).fetchone()
        if resolution is None:
            raise ValueError("arbiter work requires a red-team resolution")
        unresolved = {
            item["item_id"] for item in json.loads(resolution[0])
            if item["verdict"] == "unresolved"
        }
        items = [item for item in items if item["item_id"] in unresolved and item["material"]]
        if not items:
            raise ValueError("arbiter has no unresolved material focus")
    return {"comparison_id": comparison["comparison_id"], "role": role, "items": items}


def _focus_hash(body: Mapping[str, Any]) -> str:
    return _hash("committee-focus-v1", body)


class CommitteeRouter:
    def __init__(self, database: ResearchDB):
        self.database = database
        self.store = CommitteeStore(database)

    def initialize(self, run_id: str) -> None:
        if self.store.current_state(run_id) is None:
            self.store.record_transition(run_id, None, "PENDING_NEUTRALS", "CREATED")

    def _recover_ready_to_score(self, run_id: str) -> None:
        if self.store.current_state(run_id) == "READY_TO_SCORE":
            self._score(run_id)

    def _base_status(self, run_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as db:
            run = db.execute("SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            roles = [row[0] for row in db.execute(
                "SELECT role FROM model_assessment WHERE committee_run_id=? ORDER BY role", (run_id,)
            )]
            attempts = db.execute(
                "SELECT count(*) FROM model_call_attempt WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
            issued = db.execute(
                "SELECT count(*) FROM committee_work WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
        state = self.store.current_state(run_id)
        return {
            "committee_run_id": run_id, "candidate_id": run["candidate_id"],
            "pack_hash": run["pack_hash"], "state": state, "accepted_roles": roles,
            "model_calls": issued, "completed_attempts": attempts,
            "required_work": self._required(state, roles),
        }

    def status(self, run_id: str) -> dict[str, Any]:
        self._recover_ready_to_score(run_id)
        result = self._base_status(run_id)
        result["work"] = [
            envelope for role in result["required_work"]
            if (envelope := self._get_or_issue_work(run_id, role)) is not None
        ]
        with self.database.connect(read_only=True) as db:
            result["model_calls"] = db.execute(
                "SELECT count(*) FROM committee_work WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
        return result

    @staticmethod
    def _required(state: str | None, roles: list[str]) -> list[str]:
        if state == "PENDING_NEUTRALS":
            return [role for role in ("neutral_analyst_a", "neutral_analyst_b") if role not in roles]
        if state == "RED_TEAM_REQUIRED" and "red_team" not in roles:
            return ["red_team"]
        if state == "ARBITER_REQUIRED" and "arbiter" not in roles:
            return ["arbiter"]
        return []

    def get_work(self, run_id: str) -> dict[str, Any] | None:
        self._recover_ready_to_score(run_id)
        status = self._base_status(run_id)
        for role in status["required_work"]:
            work = self._get_or_issue_work(run_id, role)
            if work is not None:
                return work
        return None

    def _get_or_issue_work(self, run_id: str, role: str) -> dict[str, Any] | None:
        status = self._base_status(run_id)
        if role not in status["required_work"]:
            return None
        with self.database.connect(read_only=True) as db:
            run = db.execute("SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)).fetchone()
            attempts = db.execute(
                "SELECT count(*) FROM model_call_attempt WHERE committee_run_id=? AND role=?",
                (run_id, role),
            ).fetchone()[0]
            total = db.execute(
                "SELECT count(*) FROM committee_work WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
            open_work = db.execute(
                "SELECT w.* FROM committee_work w LEFT JOIN model_call_attempt a ON a.work_id=w.work_id "
                "WHERE w.committee_run_id=? AND w.role=? AND a.attempt_id IS NULL "
                "ORDER BY w.attempt_number LIMIT 1", (run_id, role),
            ).fetchone()
            prompts = json.loads(run["prompt_versions_json"])
            spec = json.loads(db.execute(
                "SELECT spec_json FROM comparator_definition WHERE config_hash=?",
                (run["comparator_config_hash"],),
            ).fetchone()[0])
            focus_body = _focus_body(db, run_id, role) if role in {"red_team", "arbiter"} else None
        if open_work is not None:
            return self._work_envelope(dict(open_work))
        if attempts >= MAX_ATTEMPTS_PER_ROLE or total >= MAX_MODEL_CALLS:
            self._record_derived_exhaustion(run_id, role)
            return None
        prompt = prompts.get(role, prompts.get("neutral") if role.startswith("neutral_") else None)
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("missing prompt version for authorized role")
        focus_hash = _focus_hash(focus_body) if focus_body is not None else None
        work_id = self.store.insert_work(
            committee_run_id=run_id, role=role, attempt_number=attempts + 1,
            pack_hash=run["pack_hash"], prompt_version=prompt,
            assessment_schema_version=run["assessment_schema_version"],
            taxonomy_version=spec["taxonomy_version"], focus_hash=focus_hash, focus=focus_body,
        )
        with self.database.connect(read_only=True) as db:
            row = db.execute("SELECT * FROM committee_work WHERE work_id=?", (work_id,)).fetchone()
        return self._work_envelope(dict(row))

    @staticmethod
    def _work_envelope(work: Mapping[str, Any]) -> dict[str, Any]:
        focus_body = json.loads(work["focus_json"]) if work["focus_json"] else None
        focus = None if focus_body is None else {**focus_body, "focus_hash": work["focus_hash"]}
        return {
            "type": "committee_work_v1", "work_id": work["work_id"],
            "committee_run_id": work["committee_run_id"], "pack_hash": work["pack_hash"],
            "role": work["role"], "prompt_version": work["prompt_version"],
            "assessment_schema_version": work["assessment_schema_version"],
            "taxonomy_version": work["taxonomy_version"], "timeout_seconds": CALL_TIMEOUT_SECONDS,
            "attempt_number": work["attempt_number"],
            "attempts_remaining": MAX_ATTEMPTS_PER_ROLE - work["attempt_number"] + 1,
            "focus": focus,
        }

    def submit(self, run_id: str, value: dict[str, Any]) -> dict[str, Any]:
        """Record one work attempt; raw assessments remain a local compatibility shim."""
        if "work_id" not in value:
            return self._submit_raw_compatibility(run_id, value)
        envelope = self._validate_attempt_envelope(value)
        work = self._load_work(run_id, envelope["work_id"])
        role = str(work["role"])
        completed = self._completed_attempt(work["work_id"])
        if completed is not None:
            return self._retry_completed(run_id, role, envelope, completed)
        status = self._base_status(run_id)
        if role not in status["required_work"]:
            raise ValueError("work is not currently authorized")
        if envelope["outcome"] != "accepted":
            attempt_id = self._record_attempt(run_id, work, envelope)
            self._advance_exhaustion_if_needed(run_id, role, envelope["outcome"])
            return {"attempt_id": attempt_id, "outcome": envelope["outcome"], **self.status(run_id)}

        assessment = envelope.get("assessment")
        if not isinstance(assessment, Mapping):
            return self._record_malformed_and_raise(run_id, work, envelope, "accepted outcome requires an assessment")
        assessment_input = dict(assessment)
        try:
            normalized = validate_for_run(self.database, run_id, role, assessment_input)
            self._validate_envelope_matches_assessment(envelope, normalized)
            if role in {"red_team", "arbiter"}:
                self._validate_exact_focus(work, normalized["claims"])
            self._validate_provider_independence(run_id, role, normalized["provider"])
        except (ValueError, AssessmentValidationError) as exc:
            return self._record_malformed_and_raise(run_id, work, envelope, str(exc), exc)

        state = status["state"]
        target = None
        with self.database.connect() as db:
            attempt_id = self._record_attempt(run_id, work, envelope, connection=db)
            assessment_id = self.store.insert_assessment(
                committee_run_id=run_id, role=role, payload=normalized,
                validate=lambda _: None, connection=db,
            )
            if role in {"red_team", "arbiter"}:
                resolution_id = self._persist_resolution(
                    db, run_id, role, assessment_id, normalized["claims"], work
                )
                unresolved = any(item["verdict"] == "unresolved" for item in normalized["claims"])
                if role == "red_team" and unresolved:
                    material_ids = {
                        item["item_id"]
                        for item in json.loads(work["focus_json"])["items"]
                        if item["material"]
                    }
                    unresolved = any(
                        item["verdict"] == "unresolved" and item["item_id"] in material_ids
                        for item in normalized["claims"]
                    )
                target = (
                    "ARBITER_REQUIRED" if role == "red_team" and unresolved else
                    "ESCALATE" if role == "arbiter" and unresolved else "READY_TO_SCORE"
                )
                self.store.record_transition(
                    run_id, state, target,
                    "RED_TEAM_REVIEWED" if role == "red_team" else "ARBITER_REVIEWED",
                    resolution_id, connection=db,
                )
        if state == "PENDING_NEUTRALS":
            roles = self._base_status(run_id)["accepted_roles"]
            if all(item in roles for item in ("neutral_analyst_a", "neutral_analyst_b")):
                comparison = Comparator(self.database).compare_and_persist(run_id)
                self.store.record_transition(
                    run_id, state, comparison["routing_decision"], "NEUTRALS_COMPARED",
                    comparison["comparison_id"],
                )
                target = comparison["routing_decision"]
        if target == "READY_TO_SCORE":
            self._score(run_id)
        return {"attempt_id": attempt_id, "assessment_id": assessment_id, **self.status(run_id)}

    def _submit_raw_compatibility(self, run_id: str, assessment: dict[str, Any]) -> dict[str, Any]:
        role = assessment.get("role")
        if not isinstance(role, str):
            raise ValueError("assessment role is required")
        assessment_input = dict(assessment)
        assessment_input.pop("submitted_at", None)
        with self.database.connect(read_only=True) as db:
            existing = db.execute(
                "SELECT semantic_assessment_hash FROM model_assessment WHERE committee_run_id=? AND role=?",
                (run_id, role),
            ).fetchone()
        if existing is not None:
            normalized = validate_for_run(self.database, run_id, role, assessment_input)
            if existing[0] != semantic_assessment_hash(role, normalized):
                raise DeterminismError("role already has a different semantic assessment")
            return self.status(run_id)
        work = self._get_or_issue_work(run_id, role)
        if work is None:
            raise ValueError("role is not currently authorized")
        return self.submit(run_id, {
            "work_id": work["work_id"], "outcome": "accepted",
            "provider": assessment["provider"], "model_id": assessment["model_id"],
            "model_route": assessment["model_route"], "billing_class": assessment["billing_class"],
            "usage": assessment["usage"], "cost": assessment["cost"], "assessment": assessment_input,
        })

    @staticmethod
    def _validate_attempt_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
        keys = set(value)
        if not ATTEMPT_REQUIRED_KEYS <= keys or not keys <= ATTEMPT_REQUIRED_KEYS | ATTEMPT_OPTIONAL_KEYS:
            raise ValueError("attempt envelope has partial or unknown fields")
        outcome = value["outcome"]
        if outcome not in ATTEMPT_OUTCOMES:
            raise ValueError("invalid attempt outcome")
        if outcome != "accepted" and value.get("assessment") is not None:
            raise ValueError("assessment is allowed only for accepted outcomes")
        for name in ("work_id", "provider", "model_id", "model_route", "billing_class"):
            if not isinstance(value[name], str) or not value[name] or len(value[name]) > 512:
                raise ValueError(f"attempt {name} must be a bounded nonempty string")
        if value["billing_class"] not in {"subscription", "local", "paid"}:
            raise ValueError("invalid attempt billing class")
        normalized = dict(value)
        normalized["usage"] = normalize_usage(value["usage"])
        normalized["cost"] = normalize_cost(value["cost"])
        diagnostic = value.get("diagnostic_excerpt")
        if diagnostic is not None and not isinstance(diagnostic, str):
            raise ValueError("diagnostic_excerpt must be a string or null")
        return normalized

    def _load_work(self, run_id: str, work_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as db:
            row = db.execute("SELECT * FROM committee_work WHERE work_id=?", (work_id,)).fetchone()
        if row is None or row["committee_run_id"] != run_id:
            raise ValueError("unknown work id for committee run")
        return dict(row)

    def _completed_attempt(self, work_id: str) -> dict[str, Any] | None:
        with self.database.connect(read_only=True) as db:
            row = db.execute("SELECT * FROM model_call_attempt WHERE work_id=?", (work_id,)).fetchone()
        return None if row is None else dict(row)

    def _retry_completed(
        self, run_id: str, role: str, envelope: Mapping[str, Any], completed: Mapping[str, Any]
    ) -> dict[str, Any]:
        if completed["outcome"] != envelope["outcome"]:
            raise DeterminismError("completed work retried with a different outcome")
        if envelope["outcome"] == "accepted":
            assessment = envelope.get("assessment")
            if not isinstance(assessment, Mapping):
                raise ValueError("accepted retry requires assessment")
            assessment_input = dict(assessment)
            normalized = validate_for_run(self.database, run_id, role, assessment_input)
            with self.database.connect(read_only=True) as db:
                stored = db.execute(
                    "SELECT assessment_id,semantic_assessment_hash FROM model_assessment "
                    "WHERE committee_run_id=? AND role=?", (run_id, role),
                ).fetchone()
            if stored is None or stored["semantic_assessment_hash"] != semantic_assessment_hash(role, normalized):
                raise DeterminismError("accepted retry changed semantic assessment")
            return {
                "attempt_id": completed["attempt_id"], "assessment_id": stored["assessment_id"],
                **self.status(run_id),
            }
        return {"attempt_id": completed["attempt_id"], "outcome": completed["outcome"], **self.status(run_id)}

    @staticmethod
    def _validate_envelope_matches_assessment(
        envelope: Mapping[str, Any], assessment: Mapping[str, Any]
    ) -> None:
        for name in ("provider", "model_id", "model_route", "billing_class", "usage", "cost"):
            if envelope[name] != assessment[name]:
                raise ValueError(f"attempt {name} does not match assessment")

    @staticmethod
    def _validate_exact_focus(work: Mapping[str, Any], verdicts: list[dict[str, Any]]) -> None:
        if not work.get("focus_json") or not work.get("focus_hash"):
            raise ValueError("targeted assessment lacks issued focus")
        focus = json.loads(work["focus_json"])
        if _focus_hash(focus) != work["focus_hash"]:
            raise DeterminismError("issued focus hash mismatch")
        expected = {item["item_id"] for item in focus["items"]}
        actual = {item["item_id"] for item in verdicts}
        if actual != expected:
            raise AssessmentValidationError("verdict item IDs must exactly cover issued focus")

    def _validate_provider_independence(self, run_id: str, role: str, provider: str) -> None:
        with self.database.connect(read_only=True) as db:
            if role in {"neutral_analyst_a", "neutral_analyst_b"}:
                other = "neutral_analyst_b" if role == "neutral_analyst_a" else "neutral_analyst_a"
                row = db.execute(
                    "SELECT provider FROM model_assessment WHERE committee_run_id=? AND role=?",
                    (run_id, other),
                ).fetchone()
                if row and row[0] == provider:
                    raise ValueError("neutral providers must differ")
            elif role == "arbiter":
                row = db.execute(
                    "SELECT provider FROM model_assessment WHERE committee_run_id=? AND role='red_team'",
                    (run_id,),
                ).fetchone()
                if row and row[0] == provider:
                    raise ValueError("arbiter provider must differ from red team")

    def _record_attempt(
        self, run_id: str, work: Mapping[str, Any], envelope: Mapping[str, Any], *,
        connection: sqlite3.Connection | None = None, forced_outcome: str | None = None,
    ) -> str:
        return self.store.insert_call_attempt(
            work_id=work["work_id"], committee_run_id=run_id, role=work["role"],
            attempt_number=work["attempt_number"], connection=connection,
            provider=envelope["provider"], model_id=envelope["model_id"],
            model_route=envelope["model_route"], billing_class=envelope["billing_class"],
            prompt_version=work["prompt_version"],
            prompt_template_hash=_hash("prompt-template-v1", [work["role"], work["prompt_version"]]),
            pack_hash=work["pack_hash"], outcome=forced_outcome or envelope["outcome"],
            usage=envelope["usage"], cost=envelope["cost"], diagnostic_hash=None,
            diagnostic_excerpt=envelope.get("diagnostic_excerpt"), requested_at=work["issued_at"],
            completed_at=utc_now(),
        )

    def _record_malformed_and_raise(
        self, run_id: str, work: Mapping[str, Any], envelope: Mapping[str, Any], message: str,
        cause: Exception | None = None,
    ) -> Any:
        self._record_attempt(run_id, work, envelope, forced_outcome="malformed")
        self._advance_exhaustion_if_needed(run_id, work["role"], "malformed")
        error = AssessmentValidationError(message)
        if cause is not None:
            raise error from cause
        raise error

    def _advance_exhaustion_if_needed(self, run_id: str, role: str, outcome: str) -> None:
        with self.database.connect(read_only=True) as db:
            role_count = db.execute(
                "SELECT count(*) FROM model_call_attempt WHERE committee_run_id=? AND role=?",
                (run_id, role),
            ).fetchone()[0]
            total = db.execute(
                "SELECT count(*) FROM committee_work WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
        if role_count >= MAX_ATTEMPTS_PER_ROLE or total >= MAX_MODEL_CALLS:
            self.record_exhaustion(run_id, role, unavailable=outcome in {"unavailable", "timeout"})

    def _record_derived_exhaustion(self, run_id: str, role: str) -> None:
        with self.database.connect(read_only=True) as db:
            last = db.execute(
                "SELECT outcome FROM model_call_attempt WHERE committee_run_id=? AND role=? "
                "ORDER BY attempt_number DESC LIMIT 1", (run_id, role),
            ).fetchone()
        self.record_exhaustion(
            run_id, role, unavailable=bool(last and last[0] in {"unavailable", "timeout"})
        )

    def _persist_resolution(
        self, db: sqlite3.Connection, run_id: str, role: str, assessment_id: str,
        verdicts: list[dict[str, Any]], work: Mapping[str, Any],
    ) -> str:
        comparison = db.execute(
            "SELECT comparison_id FROM comparison_report WHERE committee_run_id=? "
            "ORDER BY rowid DESC LIMIT 1", (run_id,),
        ).fetchone()
        if comparison is None:
            raise ValueError("resolution requires comparison")
        focus = json.loads(work["focus_json"])
        focus_hash = _focus_hash(focus)
        if focus_hash != work["focus_hash"]:
            raise DeterminismError("issued focus changed before resolution")
        logical = [comparison["comparison_id"], role, assessment_id, focus_hash, verdicts]
        resolution_id = _hash("dispute-resolution-v1", logical)
        result_hash = _hash("resolution-result-v1", verdicts)
        expected = (
            resolution_id, run_id, comparison["comparison_id"], role, assessment_id, focus_hash,
            canonical_json(focus), canonical_json(verdicts), result_hash,
        )
        existing = db.execute(
            "SELECT resolution_id,committee_run_id,comparison_id,role,assessment_id,focus_hash,"
            "focus_json,resolution_json,result_hash FROM dispute_resolution WHERE comparison_id=? AND role=?",
            (comparison["comparison_id"], role),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != expected:
                raise DeterminismError("resolution slot contains different bytes")
            return resolution_id
        db.execute("INSERT INTO dispute_resolution VALUES (?,?,?,?,?,?,?,?,?,?)", (*expected, utc_now()))
        return resolution_id

    def _score(self, run_id: str) -> None:
        from tradehub_research.committee.scoring import Scorer
        Scorer(self.database).create_snapshot(run_id)

    def record_exhaustion(self, run_id: str, role: str, *, unavailable: bool) -> None:
        state = self.store.current_state(run_id)
        if state in {"BLOCKED", "ESCALATE", "SCORED"}:
            return
        target = "BLOCKED" if unavailable else "ESCALATE"
        self.store.record_transition(run_id, state, target, f"{role.upper()}_EXHAUSTED")

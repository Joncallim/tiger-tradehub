"""Committee state machine and server-authorized work issuance."""

# ruff: noqa: E501 -- SQL projections mirror immutable row layouts.

from __future__ import annotations

import hashlib
import json
from typing import Any

from tradehub_research.committee.assessment import validate_for_run
from tradehub_research.committee.comparator import Comparator
from tradehub_research.committee.store import CommitteeStore
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.screens import canonical_json

MAX_ATTEMPTS_PER_ROLE = 2
MAX_ACCEPTED_ROLES = 4
MAX_MODEL_CALLS = 8
CALL_TIMEOUT_SECONDS = 120


class CommitteeRouter:
    def __init__(self, database: ResearchDB):
        self.database = database
        self.store = CommitteeStore(database)

    def initialize(self, run_id: str) -> None:
        if self.store.current_state(run_id) is None:
            self.store.record_transition(run_id, None, "PENDING_NEUTRALS", "CREATED")

    def status(self, run_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as db:
            run = db.execute(
                "SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            roles = [
                row[0]
                for row in db.execute(
                    "SELECT role FROM model_assessment WHERE committee_run_id=? ORDER BY role",
                    (run_id,),
                )
            ]
            attempts = db.execute(
                "SELECT count(*) FROM model_call_attempt WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
        state = self.store.current_state(run_id)
        return {
            "committee_run_id": run_id,
            "candidate_id": run["candidate_id"],
            "pack_hash": run["pack_hash"],
            "state": state,
            "accepted_roles": roles,
            "model_calls": attempts,
            "required_work": self._required(state, roles),
        }

    @staticmethod
    def _required(state: str | None, roles: list[str]) -> list[str]:
        if state == "PENDING_NEUTRALS":
            return [
                role for role in ("neutral_analyst_a", "neutral_analyst_b") if role not in roles
            ]
        if state == "RED_TEAM_REQUIRED" and "red_team" not in roles:
            return ["red_team"]
        if state == "ARBITER_REQUIRED" and "arbiter" not in roles:
            return ["arbiter"]
        return []

    def get_work(self, run_id: str) -> dict[str, Any] | None:
        status = self.status(run_id)
        required = status["required_work"]
        if not required:
            return None
        role = required[0]  # never disclose both neutral payloads in one response
        with self.database.connect(read_only=True) as db:
            run = db.execute(
                "SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)
            ).fetchone()
            attempts = db.execute(
                "SELECT count(*) FROM model_call_attempt WHERE committee_run_id=? AND role=?",
                (run_id, role),
            ).fetchone()[0]
            total = db.execute(
                "SELECT count(*) FROM model_call_attempt WHERE committee_run_id=?", (run_id,)
            ).fetchone()[0]
            if attempts >= MAX_ATTEMPTS_PER_ROLE or total >= MAX_MODEL_CALLS:
                return None
            prompts = json.loads(run["prompt_versions_json"])
            spec = json.loads(
                db.execute(
                    "SELECT spec_json FROM comparator_definition WHERE config_hash=?",
                    (run["comparator_config_hash"],),
                ).fetchone()[0]
            )
            focus = None
            if role in {"red_team", "arbiter"}:
                comparison = db.execute(
                    "SELECT report_json FROM comparison_report WHERE committee_run_id=? ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if comparison:
                    buckets = json.loads(comparison[0])
                    flagged = (
                        buckets.get("EVIDENCE_CONFLICT", [])
                        + buckets.get("CONTRADICTORY", [])
                        + buckets.get("UNSUPPORTED", [])
                        + buckets.get("OMITTED", [])
                    )
                    focus = {
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "claim_a": item.get("claim_a"),
                                "claim_b": item.get("claim_b"),
                            }
                            for item in flagged
                        ]
                    }
                    focus["focus_hash"] = hashlib.sha256(
                        ("committee-focus-v1\0" + canonical_json(focus["items"])).encode()
                    ).hexdigest()
        prompt = prompts.get(role, prompts.get("neutral") if role.startswith("neutral_") else None)
        return {
            "type": "committee_work_v1",
            "committee_run_id": run_id,
            "pack_hash": run["pack_hash"],
            "role": role,
            "prompt_version": prompt,
            "assessment_schema_version": run["assessment_schema_version"],
            "taxonomy_version": spec["taxonomy_version"],
            "timeout_seconds": CALL_TIMEOUT_SECONDS,
            "attempts_remaining": MAX_ATTEMPTS_PER_ROLE - attempts,
            "focus": focus,
        }

    def submit(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.store.current_state(run_id)
        allowed = self._required(state, self.status(run_id)["accepted_roles"])
        role = payload.get("role")
        if role not in allowed:
            raise ValueError("role is not currently authorized")
        normalized = validate_for_run(self.database, run_id, role, payload)
        if role == "neutral_analyst_b":
            with self.database.connect(read_only=True) as db:
                analyst_a = db.execute(
                    "SELECT provider FROM model_assessment WHERE committee_run_id=? "
                    "AND role='neutral_analyst_a'",
                    (run_id,),
                ).fetchone()
            if analyst_a and analyst_a[0] == payload["provider"]:
                raise ValueError("neutral providers must differ")
        if role == "arbiter":
            with self.database.connect(read_only=True) as db:
                red_provider = db.execute(
                    "SELECT provider FROM model_assessment WHERE committee_run_id=? "
                    "AND role='red_team'",
                    (run_id,),
                ).fetchone()
            if red_provider and red_provider[0] == payload["provider"]:
                raise ValueError("arbiter provider must differ from red team")
        assessment_id = self.store.insert_assessment(
            committee_run_id=run_id, role=role, payload=normalized, validate=lambda _: None
        )
        if state == "PENDING_NEUTRALS":
            roles = self.status(run_id)["accepted_roles"]
            if all(item in roles for item in ("neutral_analyst_a", "neutral_analyst_b")):
                comparison = Comparator(self.database).compare_and_persist(run_id)
                self.store.record_transition(
                    run_id,
                    state,
                    comparison["routing_decision"],
                    "NEUTRALS_COMPARED",
                    comparison["comparison_id"],
                )
                if comparison["routing_decision"] == "READY_TO_SCORE":
                    self._score(run_id)
        elif state == "RED_TEAM_REQUIRED":
            # A targeted accepted verdict either closes all focus items or requests the arbiter.
            verdicts = payload["claims"]
            self._persist_resolution(run_id, role, assessment_id, verdicts)
            unresolved = any(item["verdict"] == "unresolved" for item in verdicts)
            target = "ARBITER_REQUIRED" if unresolved else "READY_TO_SCORE"
            self.store.record_transition(run_id, state, target, "RED_TEAM_REVIEWED", assessment_id)
            if target == "READY_TO_SCORE":
                self._score(run_id)
        elif state == "ARBITER_REQUIRED":
            verdicts = payload["claims"]
            self._persist_resolution(run_id, role, assessment_id, verdicts)
            unresolved = any(item["verdict"] == "unresolved" for item in verdicts)
            target = "ESCALATE" if unresolved else "READY_TO_SCORE"
            self.store.record_transition(run_id, state, target, "ARBITER_REVIEWED", assessment_id)
            if target == "READY_TO_SCORE":
                self._score(run_id)
        return {"assessment_id": assessment_id, **self.status(run_id)}

    def _score(self, run_id: str) -> None:
        from tradehub_research.committee.scoring import Scorer

        Scorer(self.database).create_snapshot(run_id)

    def _persist_resolution(
        self, run_id: str, role: str, assessment_id: str, verdicts: list[dict[str, Any]]
    ) -> str:
        with self.database.connect() as db:
            comparison = db.execute(
                "SELECT comparison_id,report_json FROM comparison_report WHERE committee_run_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if comparison is None:
                raise ValueError("resolution requires comparison")
            buckets = json.loads(comparison["report_json"])
            items = [item for values in buckets.values() for item in values]
            allowed = {item["item_id"] for item in items}
            if not {item["item_id"] for item in verdicts} <= allowed:
                raise ValueError("verdict references an unflagged item")
            focus_hash = hashlib.sha256(
                ("committee-focus-v1\0" + canonical_json(sorted(allowed))).encode()
            ).hexdigest()
            logical = [comparison["comparison_id"], role, assessment_id, verdicts]
            resolution_id = hashlib.sha256(
                ("dispute-resolution-v1\0" + canonical_json(logical)).encode()
            ).hexdigest()
            result_hash = hashlib.sha256(
                ("resolution-result-v1\0" + canonical_json(verdicts)).encode()
            ).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO dispute_resolution VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    resolution_id,
                    run_id,
                    comparison["comparison_id"],
                    role,
                    assessment_id,
                    focus_hash,
                    canonical_json(verdicts),
                    result_hash,
                    utc_now(),
                ),
            )
        return resolution_id

    def record_exhaustion(self, run_id: str, role: str, *, unavailable: bool) -> None:
        state = self.store.current_state(run_id)
        target = "BLOCKED" if unavailable else "ESCALATE"
        self.store.record_transition(run_id, state, target, f"{role.upper()}_EXHAUSTED")

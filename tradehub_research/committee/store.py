"""Append-only persistence for Phase 2 committee runs and artifacts."""

from __future__ import annotations

# ruff: noqa: E501 -- long SQL projections mirror immutable row layouts.
import hashlib
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tradehub_research.committee.assessment import normalize_cost, normalize_usage
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

NEUTRAL_ROLES = ("neutral_analyst_a", "neutral_analyst_b")
CLAIM_TAXONOMY = (
    "valuation_vs_history",
    "valuation_vs_peers",
    "valuation_vs_growth",
    "cash_flow_valuation",
    "balance_sheet_strength",
    "earnings_quality",
    "margin_durability",
    "capital_allocation_quality",
    "accounting_risk",
    "governance_risk",
    "revenue_inflection",
    "margin_inflection",
    "demand_inflection",
    "cost_structure_change",
    "catalyst_upcoming",
    "event_repricing",
    "regulatory_event",
    "litigation_event",
    "corporate_action",
    "insider_activity_signal",
    "institutional_positioning",
    "short_interest_signal",
    "price_trend_quality",
    "relative_strength",
    "volume_confirmation",
    "liquidity_risk",
    "concentration_risk",
    "macro_sensitivity",
    "data_freshness_risk",
    "other",
)


def _logical_id(domain: str, value: object) -> str:
    return hashlib.sha256((domain + "\0" + canonical_json(value)).encode()).hexdigest()


def semantic_assessment_payload(role: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project validated model content onto its telemetry-free logical identity."""
    return {
        "role": role,
        "pack_hash": payload["pack_hash"],
        "prompt_version": payload["prompt_version"],
        "assessment_schema_version": payload["assessment_schema_version"],
        "taxonomy_version": payload["taxonomy_version"],
        "claims": payload["claims"],
        "cited_evidence_ids": payload["cited_evidence_ids"],
        "missing_evidence": payload["missing_evidence"],
        "thesis": payload["thesis"],
        "confidence": payload["confidence"],
        "uncertainty": payload["uncertainty"],
    }


def semantic_assessment_hash(role: str, payload: Mapping[str, Any]) -> str:
    return _logical_id("semantic-assessment-v1", semantic_assessment_payload(role, payload))


def _sanitized_diagnostic(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str):
        raise ValueError("diagnostic_excerpt must be a string or null")
    cleaned = re.sub(r"(?i)bearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    cleaned = re.sub(
        r"(?i)(token|secret|authorization|private[_ -]?key)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        cleaned,
    )[:256]
    return hashlib.sha256(cleaned.encode()).hexdigest(), cleaned


@dataclass(frozen=True)
class ComparatorSpec:
    comparator_version: int = 1
    taxonomy_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "comparator_version": self.comparator_version,
            "taxonomy_version": self.taxonomy_version,
            "taxonomy": list(CLAIM_TAXONOMY),
            "bounds": {
                "claims_total": 12,
                "material_claims": 8,
                "missing_evidence": 8,
                "thesis_break_conditions": 6,
                "string_codepoints": 512,
            },
            "material_threshold": 3,
            "evidence_jaccard_threshold": "0.5",
            "materiality_delta_flag": 2,
            "uncertainty_delta_flag": "0.4",
            "routing": {
                "agreement_threshold": "0.34",
                "agreement_min_material_union": 3,
                "unsupported_threshold": 2,
                "omission_imbalance_threshold": 2,
            },
        }

    @property
    def spec_json(self) -> str:
        return canonical_json(self.as_dict())

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.spec_json.encode()).hexdigest()


@dataclass(frozen=True)
class ScoringSpec:
    scoring_version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "scoring_version": self.scoring_version,
            "weights": {
                "valuation": 18,
                "inflection": 18,
                "quality": 18,
                "informed_activity": 18,
                "event": 12,
                "momentum_confirmation": 6,
            },
            "confluence": {
                "formula": "5*min(2,max(0,N-1))",
                "N": "source_id components linked by shared cluster_ids",
                "component_tokens": "independence:v1:<canonical sorted source_id JSON array>",
                "missing_source_id": "excluded",
                "cap": 10,
            },
            "missing_penalty": {"formula": "min(10,2*count)", "cap": 10},
            "staleness_penalty": {"formula": "min(6,2*count)", "cap": 6},
            "conviction_bucket": 5,
            "decimal_places": 6,
            "rounding": "round_half_up",
        }

    @property
    def spec_json(self) -> str:
        return canonical_json(self.as_dict())

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.spec_json.encode()).hexdigest()


class CommitteeStore:
    def __init__(self, database: ResearchDB):
        self.database = database

    def ensure_registry_rows(self) -> tuple[str, str]:
        comparator, scoring = ComparatorSpec(), ScoringSpec()
        with self.database.connect() as db:
            self._ensure_registry(
                db,
                "comparator_definition",
                "comparator_version",
                comparator.comparator_version,
                comparator.config_hash,
                comparator.spec_json,
                (comparator.taxonomy_version,),
            )
            self._ensure_registry(
                db,
                "scoring_version",
                "scoring_version",
                scoring.scoring_version,
                scoring.config_hash,
                scoring.spec_json,
                ("Phase 2 scoring v1",),
            )
        return comparator.config_hash, scoring.config_hash

    @staticmethod
    def _ensure_registry(
        db: sqlite3.Connection,
        table: str,
        version_column: str,
        version: int,
        config_hash: str,
        spec_json: str,
        extra: tuple[Any, ...],
    ) -> None:
        row = db.execute(
            f"SELECT config_hash,spec_json FROM {table} WHERE {version_column}=?", (version,)
        ).fetchone()
        if row is not None:
            if tuple(row) != (config_hash, spec_json):
                raise DeterminismError(f"{table} version reused with different bytes")
            return
        if table == "comparator_definition":
            db.execute(
                "INSERT INTO comparator_definition(config_hash,comparator_version,taxonomy_version,spec_json,created_at) VALUES (?,?,?,?,?)",
                (config_hash, version, extra[0], spec_json, utc_now()),
            )
        else:
            db.execute(
                "INSERT INTO scoring_version(config_hash,scoring_version,spec_json,description,created_at) VALUES (?,?,?,?,?)",
                (config_hash, version, spec_json, extra[0], utc_now()),
            )

    def create_or_resume_committee_run(
        self,
        *,
        candidate_id: str,
        pack_hash: str,
        committee_policy_version: int,
        comparator_config_hash: str,
        scoring_config_hash: str,
        prompt_versions: Mapping[str, Any],
        assessment_schema_version: int,
        provider_routes: object | None = None,
    ) -> str:
        del provider_routes
        roles = list(NEUTRAL_ROLES)
        logical = {
            "candidate_id": candidate_id,
            "pack_hash": pack_hash,
            "roles": roles,
            "committee_policy_version": committee_policy_version,
            "comparator_config_hash": comparator_config_hash,
            "scoring_config_hash": scoring_config_hash,
            "prompt_versions": dict(prompt_versions),
            "assessment_schema_version": assessment_schema_version,
        }
        run_id = _logical_id("committee-run-v1", logical)
        values = (
            run_id,
            candidate_id,
            pack_hash,
            canonical_json(roles),
            committee_policy_version,
            comparator_config_hash,
            scoring_config_hash,
            canonical_json(dict(prompt_versions)),
            assessment_schema_version,
        )
        with self.database.connect() as db:
            candidate = db.execute(
                "SELECT run_id FROM candidate WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            pack = db.execute(
                "SELECT pipeline_run_id,candidate_id FROM evidence_pack WHERE pack_hash=?",
                (pack_hash,),
            ).fetchone()
            if (
                candidate is None
                or pack is None
                or pack[1] != candidate_id
                or pack[0] != candidate[0]
            ):
                raise ValueError("committee run candidate/pack mismatch")
            stored = db.execute(
                "SELECT committee_run_id,candidate_id,pack_hash,role_set_json,committee_policy_version,comparator_config_hash,scoring_config_hash,prompt_versions_json,assessment_schema_version FROM committee_run WHERE committee_run_id=?",
                (run_id,),
            ).fetchone()
            if stored is not None:
                if tuple(stored) != values:
                    raise DeterminismError("committee run identity collision")
                return run_id
            db.execute(
                "INSERT INTO committee_run(committee_run_id,candidate_id,pipeline_run_id,pack_hash,role_set_json,committee_policy_version,comparator_config_hash,scoring_config_hash,prompt_versions_json,assessment_schema_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, candidate_id, candidate[0], *values[2:], utc_now()),
            )
        return run_id

    def record_transition(
        self,
        run_id: str,
        from_state: str | None,
        to_state: str,
        cause_code: str,
        artifact_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        logical = {
            "committee_run_id": run_id,
            "from_state": from_state,
            "to_state": to_state,
            "cause_code": cause_code,
            "artifact_id": artifact_id,
        }
        transition_id = _logical_id("committee-transition-v1", logical)
        if connection is None:
            with self.database.connect() as db:
                return self.record_transition(
                    run_id,
                    from_state,
                    to_state,
                    cause_code,
                    artifact_id,
                    connection=db,
                )
        db = connection
        row = db.execute(
            "SELECT committee_run_id,from_state,to_state,cause_code,artifact_id FROM committee_transition WHERE transition_id=?",
            (transition_id,),
        ).fetchone()
        expected = (run_id, from_state, to_state, cause_code, artifact_id)
        if row is not None:
            if tuple(row) != expected:
                raise DeterminismError("transition identity collision")
            return transition_id
        current = self._current_state(db, run_id)
        if current != from_state:
            raise ValueError(f"transition expected {from_state!r}, current state is {current!r}")
        db.execute(
            "INSERT INTO committee_transition(transition_id,committee_run_id,from_state,to_state,cause_code,artifact_id,occurred_at) VALUES (?,?,?,?,?,?,?)",
            (transition_id, *expected, utc_now()),
        )
        return transition_id

    def current_state(self, run_id: str) -> str | None:
        with self.database.connect(read_only=True) as db:
            return self._current_state(db, run_id)

    @staticmethod
    def _current_state(db: sqlite3.Connection, run_id: str) -> str | None:
        if (
            db.execute("SELECT 1 FROM committee_run WHERE committee_run_id=?", (run_id,)).fetchone()
            is None
        ):
            raise KeyError(f"unknown committee run: {run_id}")
        row = db.execute(
            "SELECT to_state FROM committee_transition WHERE committee_run_id=? ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def insert_assessment(
        self,
        *,
        committee_run_id: str,
        role: str,
        payload: Mapping[str, Any],
        validate: Callable[[Mapping[str, Any]], Any],
        connection: sqlite3.Connection | None = None,
    ) -> str:
        validate(payload)  # validation precedes and shares the all-or-nothing write boundary
        semantic_hash = semantic_assessment_hash(role, payload)
        assessment_id = _logical_id(
            "model-assessment-v2",
            {"committee_run_id": committee_run_id, "semantic_assessment_hash": semantic_hash},
        )
        payload_json = canonical_json(dict(payload))
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        fields = (
            "candidate_id",
            "pack_hash",
            "provider",
            "model_id",
            "prompt_version",
            "assessment_schema_version",
            "taxonomy_version",
            "model_route",
            "billing_class",
            "claims",
            "cited_evidence_ids",
            "missing_evidence",
            "thesis",
            "confidence",
            "uncertainty",
            "usage",
            "cost",
            "evaluation_time",
            "submitted_at",
        )
        missing = [name for name in fields if name not in payload]
        if missing:
            raise ValueError(f"missing assessment fields: {', '.join(missing)}")
        values = (
            assessment_id,
            committee_run_id,
            payload["candidate_id"],
            payload["pack_hash"],
            role,
            payload["provider"],
            payload["model_id"],
            payload["prompt_version"],
            payload["assessment_schema_version"],
            payload["taxonomy_version"],
            payload["model_route"],
            payload["billing_class"],
            canonical_json(payload["claims"]),
            canonical_json(payload["cited_evidence_ids"]),
            canonical_json(payload["missing_evidence"]),
            canonical_json(payload["thesis"]),
            payload["confidence"],
            payload["uncertainty"],
            canonical_json(payload["usage"]),
            canonical_json(payload["cost"]),
            payload["evaluation_time"],
            payload["submitted_at"],
            payload_hash,
            semantic_hash,
        )
        if connection is None:
            with self.database.connect() as db:
                return self.insert_assessment(
                    committee_run_id=committee_run_id,
                    role=role,
                    payload=payload,
                    validate=lambda _: None,
                    connection=db,
                )
        db = connection
        run = db.execute(
            "SELECT candidate_id,pack_hash FROM committee_run WHERE committee_run_id=?",
            (committee_run_id,),
        ).fetchone()
        if run is None or tuple(run) != (payload["candidate_id"], payload["pack_hash"]):
            raise ValueError("assessment does not belong to committee run")
        existing = db.execute(
            "SELECT assessment_id,semantic_assessment_hash FROM model_assessment "
            "WHERE committee_run_id=? AND role=?",
            (committee_run_id, role),
        ).fetchone()
        if existing is not None:
            if existing["semantic_assessment_hash"] != semantic_hash:
                raise DeterminismError("role already has a different semantic assessment")
            return str(existing["assessment_id"])
        db.execute(
            "INSERT INTO model_assessment VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        return assessment_id

    def insert_work(
        self,
        *,
        committee_run_id: str,
        role: str,
        attempt_number: int,
        pack_hash: str,
        prompt_version: str,
        assessment_schema_version: int,
        taxonomy_version: int,
        focus_hash: str | None,
        focus: object | None,
    ) -> str:
        logical = {
            "committee_run_id": committee_run_id,
            "role": role,
            "attempt_number": attempt_number,
            "pack_hash": pack_hash,
            "prompt_version": prompt_version,
            "assessment_schema_version": assessment_schema_version,
            "taxonomy_version": taxonomy_version,
            "focus_hash": focus_hash,
            "focus": focus,
        }
        work_id = _logical_id("committee-work-v1", logical)
        expected = (
            work_id,
            committee_run_id,
            role,
            attempt_number,
            pack_hash,
            prompt_version,
            assessment_schema_version,
            taxonomy_version,
            focus_hash,
            None if focus is None else canonical_json(focus),
        )
        with self.database.connect() as db:
            run = db.execute(
                "SELECT pack_hash FROM committee_run WHERE committee_run_id=?",
                (committee_run_id,),
            ).fetchone()
            if run is None or run[0] != pack_hash:
                raise ValueError("work does not belong to committee run")
            existing = db.execute(
                "SELECT work_id,committee_run_id,role,attempt_number,pack_hash,prompt_version,"
                "assessment_schema_version,taxonomy_version,focus_hash,focus_json "
                "FROM committee_work WHERE committee_run_id=? AND role=? AND attempt_number=?",
                (committee_run_id, role, attempt_number),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != expected:
                    raise DeterminismError("work slot already contains different bytes")
                return work_id
            db.execute(
                "INSERT INTO committee_work VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (*expected, utc_now()),
            )
        return work_id

    def insert_call_attempt(
        self,
        *,
        work_id: str,
        committee_run_id: str,
        role: str,
        attempt_number: int,
        connection: sqlite3.Connection | None = None,
        **payload: Any,
    ) -> str:
        outcome = str(payload.get("outcome", "")).lower()
        if outcome not in {"accepted", "malformed", "unavailable", "timeout"}:
            raise ValueError("invalid attempt outcome")
        usage = normalize_usage(payload.get("usage"))
        cost = normalize_cost(payload.get("cost"))
        diagnostic_hash, diagnostic_excerpt = _sanitized_diagnostic(
            payload.get("diagnostic_excerpt")
        )
        payload = {
            **payload,
            "outcome": outcome,
            "usage": usage,
            "cost": cost,
            "diagnostic_hash": diagnostic_hash,
            "diagnostic_excerpt": diagnostic_excerpt,
        }
        logical = {
            "work_id": work_id,
            "committee_run_id": committee_run_id,
            "role": role,
            "attempt_number": attempt_number,
            **payload,
        }
        attempt_id = _logical_id("model-call-attempt-v1", logical)
        names = (
            "provider",
            "model_id",
            "model_route",
            "billing_class",
            "prompt_version",
            "prompt_template_hash",
            "pack_hash",
            "outcome",
            "usage",
            "cost",
            "diagnostic_hash",
            "diagnostic_excerpt",
            "requested_at",
            "completed_at",
        )
        missing = [name for name in names if name not in payload]
        if missing:
            raise ValueError(f"missing attempt fields: {', '.join(missing)}")
        values = (
            attempt_id,
            work_id,
            committee_run_id,
            role,
            attempt_number,
            payload["provider"],
            payload["model_id"],
            payload["model_route"],
            payload["billing_class"],
            payload["prompt_version"],
            payload["prompt_template_hash"],
            payload["pack_hash"],
            payload["outcome"],
            canonical_json(payload["usage"]),
            canonical_json(payload["cost"]),
            payload["diagnostic_hash"],
            payload["diagnostic_excerpt"],
            payload["requested_at"],
            payload["completed_at"],
        )
        if connection is None:
            with self.database.connect() as db:
                return self.insert_call_attempt(
                    work_id=work_id,
                    committee_run_id=committee_run_id,
                    role=role,
                    attempt_number=attempt_number,
                    connection=db,
                    **payload,
                )
        db = connection
        work = db.execute(
            "SELECT committee_run_id,role,attempt_number FROM committee_work WHERE work_id=?",
            (work_id,),
        ).fetchone()
        if work is None or tuple(work) != (committee_run_id, role, attempt_number):
            raise ValueError("attempt does not match issued work")
        row = db.execute(
            "SELECT attempt_id FROM model_call_attempt WHERE committee_run_id=? AND role=? AND attempt_number=?",
            (committee_run_id, role, attempt_number),
        ).fetchone()
        if row is not None:
            if row[0] != attempt_id:
                raise DeterminismError("attempt slot already contains different bytes")
            return attempt_id
        db.execute(
            "INSERT INTO model_call_attempt VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        return attempt_id


def ensure_registry_rows(database: ResearchDB) -> tuple[str, str]:
    return CommitteeStore(database).ensure_registry_rows()

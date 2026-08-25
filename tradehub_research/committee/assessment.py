"""Strict, atomic validation for committee assessment payloads."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from tradehub_research.db import ResearchDB

ROLES = {"neutral_analyst_a", "neutral_analyst_b", "red_team", "arbiter"}
CLAIM_TYPES = {"fact", "interpretation", "projection"}
DIRECTIONS = {"bullish", "neutral", "bearish"}
TOP_LEVEL = {
    "candidate_id",
    "pack_hash",
    "role",
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
}
CLAIM_KEYS = {
    "claim_key",
    "claim_type",
    "direction",
    "statement",
    "materiality",
    "uncertainty",
    "cited_evidence_ids",
    "contradictory_evidence_ids",
    "falsification_condition",
}
THESIS_KEYS = {"summary", "upside_mechanism", "downside_mechanism", "thesis_break_conditions"}
VERDICT_KEYS = {"item_id", "verdict", "statement", "cited_evidence_ids"}
VERDICTS = {"resolved_for_a", "resolved_for_b", "both_wrong", "unresolved"}
MISSING_EVIDENCE_KEYS = {"claim_key", "description", "materiality"}
USAGE_KEYS = {"input_tokens", "output_tokens", "cached_tokens", "source"}
COST_KEYS = {"amount", "currency", "source"}
TELEMETRY_SOURCES = {"SELF_REPORTED", "UNKNOWN"}
MAX_ASSESSMENT_BYTES = 64_000


class AssessmentValidationError(ValueError):
    """Assessment violated the exact structured contract."""


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AssessmentValidationError(f"{name} must be a finite number")
    value = float(value)
    if not 0 <= value <= 1:
        raise AssessmentValidationError(f"{name} must be between 0 and 1")
    return value


def _string(value: Any, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise AssessmentValidationError(
            f"{name} must be a nonempty string of at most 512 code points"
        )
    return value


def normalize_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != USAGE_KEYS:
        raise AssessmentValidationError("usage must contain the exact typed usage fields")
    source = value["source"]
    if source not in TELEMETRY_SOURCES:
        raise AssessmentValidationError("invalid usage source")
    normalized: dict[str, Any] = {"source": source}
    for name in ("input_tokens", "output_tokens", "cached_tokens"):
        item = value[name]
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0):
            raise AssessmentValidationError(f"usage.{name} must be a nonnegative integer or null")
        if source == "UNKNOWN" and item is not None:
            raise AssessmentValidationError("UNKNOWN usage cannot contain token counts")
        normalized[name] = item
    return normalized


def normalize_cost(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != COST_KEYS:
        raise AssessmentValidationError("cost must contain the exact typed cost fields")
    source = value["source"]
    if source not in TELEMETRY_SOURCES:
        raise AssessmentValidationError("invalid cost source")
    amount = value["amount"]
    normalized_amount: str | None
    if amount is None:
        normalized_amount = None
    elif isinstance(amount, bool) or not isinstance(amount, (str, int, float, Decimal)):
        raise AssessmentValidationError("cost.amount must be a finite nonnegative decimal or null")
    else:
        try:
            decimal = Decimal(str(amount))
        except InvalidOperation as exc:
            raise AssessmentValidationError(
                "cost.amount must be a finite nonnegative decimal or null"
            ) from exc
        if not decimal.is_finite() or decimal < 0:
            raise AssessmentValidationError("cost.amount must be finite and nonnegative")
        normalized_amount = format(decimal.normalize(), "f")
    currency = value["currency"]
    if currency is not None and (not isinstance(currency, str) or not 1 <= len(currency) <= 12):
        raise AssessmentValidationError("cost.currency must be a bounded string or null")
    if source == "UNKNOWN" and (normalized_amount is not None or currency is not None):
        raise AssessmentValidationError("UNKNOWN cost cannot contain amount or currency")
    return {"amount": normalized_amount, "currency": currency, "source": source}


def _id_list(value: Any, name: str, in_pack: set[str]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AssessmentValidationError(f"{name} must be a string array")
    if len(value) != len(set(value)):
        raise AssessmentValidationError(f"{name} contains duplicates")
    if not set(value) <= in_pack:
        raise AssessmentValidationError(f"{name} contains out-of-pack evidence")
    return list(value)


def validate_assessment(
    payload: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    pack_body: Mapping[str, Any],
    comparator_spec: Mapping[str, Any],
    expected_role: str | None = None,
) -> dict[str, Any]:
    """Return normalized bytes or reject the entire payload; never repair it."""
    if not isinstance(payload, Mapping):
        raise AssessmentValidationError("assessment must be an object")
    unknown = set(payload) - TOP_LEVEL
    missing = TOP_LEVEL - set(payload)
    if unknown or missing:
        raise AssessmentValidationError(
            f"assessment keys invalid: unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    role = payload["role"]
    if role not in ROLES or (expected_role is not None and role != expected_role):
        raise AssessmentValidationError("wrong role")
    if payload["candidate_id"] != run["candidate_id"] or payload["pack_hash"] != run["pack_hash"]:
        raise AssessmentValidationError("wrong candidate or pack hash")
    if payload["assessment_schema_version"] != run["assessment_schema_version"]:
        raise AssessmentValidationError("wrong assessment schema version")
    if payload["taxonomy_version"] != comparator_spec["taxonomy_version"]:
        raise AssessmentValidationError("wrong taxonomy version")
    prompt_versions = (
        json.loads(run["prompt_versions_json"])
        if isinstance(run["prompt_versions_json"], str)
        else run["prompt_versions_json"]
    )
    expected_prompt = prompt_versions.get(
        role, prompt_versions.get("neutral") if role.startswith("neutral_") else None
    )
    if expected_prompt is not None and payload["prompt_version"] != expected_prompt:
        raise AssessmentValidationError("wrong prompt version")
    if payload["evaluation_time"] != pack_body["run"]["as_of"]:
        raise AssessmentValidationError("evaluation time must equal pack as_of")
    for name in (
        "candidate_id",
        "pack_hash",
        "provider",
        "model_id",
        "prompt_version",
        "model_route",
        "billing_class",
        "evaluation_time",
    ):
        _string(payload[name], name)
    if payload["billing_class"] not in {"subscription", "local", "paid"}:
        raise AssessmentValidationError("invalid billing class")
    confidence = _finite_number(payload["confidence"], "confidence")
    uncertainty = _finite_number(payload["uncertainty"], "uncertainty")
    in_pack = {row["evidence_id"] for row in pack_body["evidence"]}
    claims = payload["claims"]
    bounds = comparator_spec["bounds"]
    if not isinstance(claims, list) or len(claims) > bounds["claims_total"]:
        raise AssessmentValidationError("claims bound exceeded")
    taxonomy = set(comparator_spec["taxonomy"])
    normalized_claims: list[dict[str, Any]] = []
    slots: set[tuple[str, str, str]] = set()
    material = 0
    all_citations: set[str] = set()
    if role in {"red_team", "arbiter"}:
        if not claims:
            raise AssessmentValidationError("targeted role requires verdicts")
        item_ids: set[str] = set()
        for index, verdict in enumerate(claims):
            if not isinstance(verdict, Mapping) or set(verdict) != VERDICT_KEYS:
                raise AssessmentValidationError(f"verdict {index} has partial or unknown fields")
            item_id = _string(verdict["item_id"], "item_id")
            if item_id in item_ids:
                raise AssessmentValidationError("duplicate verdict item")
            item_ids.add(item_id)
            if verdict["verdict"] not in VERDICTS:
                raise AssessmentValidationError("invalid verdict")
            _string(verdict["statement"], "verdict statement")
            cited = _id_list(verdict["cited_evidence_ids"], "verdict evidence", in_pack)
            all_citations.update(cited)
            normalized_claims.append({**dict(verdict), "cited_evidence_ids": sorted(cited)})
        claims = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping) or set(claim) != CLAIM_KEYS:
            raise AssessmentValidationError(f"claim {index} has partial or unknown fields")
        key, kind, direction = claim["claim_key"], claim["claim_type"], claim["direction"]
        if key not in taxonomy or kind not in CLAIM_TYPES or direction not in DIRECTIONS:
            raise AssessmentValidationError(f"claim {index} identity is invalid")
        slot = (key, kind, direction)
        if slot in slots:
            raise AssessmentValidationError("duplicate claim slot")
        slots.add(slot)
        if (
            isinstance(claim["materiality"], bool)
            or not isinstance(claim["materiality"], int)
            or not 1 <= claim["materiality"] <= 5
        ):
            raise AssessmentValidationError("materiality must be an integer from 1 to 5")
        if key == "other" and claim["materiality"] > 2:
            raise AssessmentValidationError("other materiality is capped at 2")
        _string(claim["statement"], "statement")
        _finite_number(claim["uncertainty"], "claim uncertainty")
        cited = _id_list(claim["cited_evidence_ids"], "cited_evidence_ids", in_pack)
        contradictory = _id_list(
            claim["contradictory_evidence_ids"], "contradictory_evidence_ids", in_pack
        )
        if set(cited) & set(contradictory):
            raise AssessmentValidationError("cited and contradictory evidence must be disjoint")
        if claim["materiality"] >= comparator_spec["material_threshold"]:
            material += 1
            if not cited:
                raise AssessmentValidationError("material claim must cite in-pack evidence")
        condition = _string(
            claim["falsification_condition"], "falsification_condition", optional=True
        )
        if kind == "projection" and condition is None:
            raise AssessmentValidationError("projection requires falsification condition")
        all_citations.update(cited)
        all_citations.update(contradictory)
        normalized_claims.append(
            {
                **dict(claim),
                "cited_evidence_ids": sorted(cited),
                "contradictory_evidence_ids": sorted(contradictory),
            }
        )
    if material > bounds["material_claims"]:
        raise AssessmentValidationError("material claims bound exceeded")
    cited_top = _id_list(payload["cited_evidence_ids"], "assessment cited_evidence_ids", in_pack)
    if set(cited_top) != all_citations:
        raise AssessmentValidationError("assessment citation index does not match claims")
    missing_evidence = payload["missing_evidence"]
    if not isinstance(missing_evidence, list) or len(missing_evidence) > bounds["missing_evidence"]:
        raise AssessmentValidationError("missing evidence bound exceeded")
    normalized_missing = []
    for index, item in enumerate(missing_evidence):
        if not isinstance(item, Mapping) or set(item) != MISSING_EVIDENCE_KEYS:
            raise AssessmentValidationError(
                f"missing_evidence {index} has partial or unknown fields"
            )
        if item["claim_key"] not in taxonomy:
            raise AssessmentValidationError("missing evidence claim key is invalid")
        _string(item["description"], "missing evidence description")
        if (
            isinstance(item["materiality"], bool)
            or not isinstance(item["materiality"], int)
            or not 1 <= item["materiality"] <= 5
        ):
            raise AssessmentValidationError("missing evidence materiality must be 1 to 5")
        normalized_missing.append(dict(item))
    thesis = payload["thesis"]
    if not isinstance(thesis, Mapping) or set(thesis) != THESIS_KEYS:
        raise AssessmentValidationError("thesis has partial or unknown fields")
    for name in ("summary", "upside_mechanism", "downside_mechanism"):
        _string(thesis[name], f"thesis.{name}")
    breaks = thesis["thesis_break_conditions"]
    if not isinstance(breaks, list) or len(breaks) > bounds["thesis_break_conditions"]:
        raise AssessmentValidationError("thesis break condition bound exceeded")
    for item in breaks:
        _string(item, "thesis break condition")
    usage = normalize_usage(payload["usage"])
    cost = normalize_cost(payload["cost"])
    normalized_claims.sort(
        key=lambda item: (
            item.get("item_id", ""),
            item.get("claim_key", ""),
            item.get("claim_type", ""),
            item.get("direction", ""),
        )
    )
    normalized_missing.sort(
        key=lambda item: (item["claim_key"], item["materiality"], item["description"])
    )
    normalized = dict(payload)
    normalized.update(
        claims=normalized_claims,
        cited_evidence_ids=sorted(cited_top),
        missing_evidence=normalized_missing,
        confidence=confidence,
        uncertainty=uncertainty,
        thesis={**dict(thesis), "thesis_break_conditions": sorted(breaks)},
        usage=usage,
        cost=cost,
    )
    from tradehub_research.db import utc_now

    normalized["submitted_at"] = utc_now()
    from tradehub_research.screens import canonical_json

    if len(canonical_json(normalized).encode()) > MAX_ASSESSMENT_BYTES:
        raise AssessmentValidationError("assessment artifact exceeds byte bound")
    return normalized


def validate_for_run(
    database: ResearchDB, run_id: str, role: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    with database.connect(read_only=True) as db:
        run = db.execute(
            "SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown committee run: {run_id}")
        pack = db.execute(
            "SELECT body_json FROM evidence_pack WHERE pack_hash=?", (run["pack_hash"],)
        ).fetchone()
        spec = db.execute(
            "SELECT spec_json FROM comparator_definition WHERE config_hash=?",
            (run["comparator_config_hash"],),
        ).fetchone()
    return validate_assessment(
        payload,
        run=dict(run),
        pack_body=json.loads(pack[0]),
        comparator_spec=json.loads(spec[0]),
        expected_role=role,
    )

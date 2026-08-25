"""Deterministic structured-claim comparator v1."""

# ruff: noqa: E501 -- SQL projections mirror immutable row layouts.

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json


def _item_id(kind: str, identity: object) -> str:
    return hashlib.sha256(
        ("comparison-item-v1\0" + kind + "\0" + canonical_json(identity)).encode()
    ).hexdigest()


def _material(claim: Mapping[str, Any], threshold: int) -> bool:
    return int(claim["materiality"]) >= threshold


def compare_assessments(
    a: Mapping[str, Any], b: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[str, Any]:
    """Partition claim identities into five disjoint buckets in normative precedence."""
    threshold = int(spec["material_threshold"])
    maps: list[dict[tuple[str, str, str], Mapping[str, Any]]] = []
    for assessment in (a, b):
        claims: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for claim in assessment["claims"]:
            key = (claim["claim_key"], claim["claim_type"], claim["direction"])
            if key in claims:
                raise ValueError("duplicate claim match key")
            claims[key] = claim
        maps.append(claims)
    ma, mb = maps
    buckets: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in ("EVIDENCE_CONFLICT", "CONTRADICTORY", "UNSUPPORTED", "SHARED", "OMITTED")
    }
    consumed_a: set[tuple[str, str, str]] = set()
    consumed_b: set[tuple[str, str, str]] = set()

    def append(
        kind: str,
        keys: object,
        ca: Mapping[str, Any] | None,
        cb: Mapping[str, Any] | None,
        **extra: Any,
    ) -> None:
        buckets[kind].append(
            {"item_id": _item_id(kind, keys), "claim_a": ca, "claim_b": cb, **extra}
        )

    # Conflicts and contradictions pair claims at key/type level, before exact-direction sharing.
    bases = sorted({key[:2] for key in ma} | {key[:2] for key in mb})
    for base in bases:
        aa = [(key, value) for key, value in ma.items() if key[:2] == base]
        bb = [(key, value) for key, value in mb.items() if key[:2] == base]
        for ka, ca in aa:
            for kb, cb in bb:
                if ka in consumed_a or kb in consumed_b:
                    continue
                sa, sb = set(ca["cited_evidence_ids"]), set(cb["cited_evidence_ids"])
                xa, xb = (
                    set(ca["contradictory_evidence_ids"]),
                    set(cb["contradictory_evidence_ids"]),
                )
                opposite = {ka[2], kb[2]} == {"bullish", "bearish"}
                conflict_ids = sorted((sa & xb) | (sb & xa) | ((sa & sb) if opposite else set()))
                if conflict_ids:
                    append(
                        "EVIDENCE_CONFLICT",
                        [ka, kb],
                        ca,
                        cb,
                        evidence_ids=conflict_ids,
                        material=_material(ca, threshold) or _material(cb, threshold),
                    )
                    consumed_a.add(ka)
                    consumed_b.add(kb)
                elif opposite and (_material(ca, threshold) or _material(cb, threshold)):
                    append("CONTRADICTORY", [ka, kb], ca, cb, material=True)
                    consumed_a.add(ka)
                    consumed_b.add(kb)

    # Unsupported is retained for comparator replay of legacy/bypassed assessment rows.
    for side, claims, consumed in (("a", ma, consumed_a), ("b", mb, consumed_b)):
        for key, claim in sorted(claims.items()):
            if (
                key not in consumed
                and _material(claim, threshold)
                and not claim["cited_evidence_ids"]
            ):
                append(
                    "UNSUPPORTED",
                    [side, key],
                    claim if side == "a" else None,
                    claim if side == "b" else None,
                    side=side,
                    material=True,
                )
                consumed.add(key)

    for key in sorted(set(ma) & set(mb)):
        if key in consumed_a or key in consumed_b:
            continue
        ca, cb = ma[key], mb[key]
        sa, sb = set(ca["cited_evidence_ids"]), set(cb["cited_evidence_ids"])
        union = sa | sb
        jaccard = len(sa & sb) / len(union) if union else 0.0
        alignment = (
            "aligned"
            if jaccard >= float(spec["evidence_jaccard_threshold"])
            else ("partial" if jaccard > 0 else "disjoint")
        )
        append(
            "SHARED",
            key,
            ca,
            cb,
            evidence_jaccard=round(jaccard, 6),
            alignment=alignment,
            material=_material(ca, threshold) or _material(cb, threshold),
            materiality_delta=abs(ca["materiality"] - cb["materiality"]),
            uncertainty_delta=round(abs(ca["uncertainty"] - cb["uncertainty"]), 6),
        )
        consumed_a.add(key)
        consumed_b.add(key)

    omissions = {"a": 0, "b": 0}
    for side, claims, consumed in (("a", ma, consumed_a), ("b", mb, consumed_b)):
        for key, claim in sorted(claims.items()):
            if key not in consumed and _material(claim, threshold):
                append(
                    "OMITTED",
                    [side, key],
                    claim if side == "a" else None,
                    claim if side == "b" else None,
                    side=side,
                    material=True,
                )
                omissions[side] += 1
                consumed.add(key)

    material_keys = {key for key, claim in ma.items() if _material(claim, threshold)} | {
        key for key, claim in mb.items() if _material(claim, threshold)
    }
    aligned = sum(
        1 for item in buckets["SHARED"] if item["material"] and item["alignment"] == "aligned"
    )
    agreement = None if not material_keys else round(aligned / max(1, len(material_keys)), 6)
    triggers: list[str] = []
    if any(item["material"] for item in buckets["CONTRADICTORY"]):
        triggers.append("R1")
    if buckets["EVIDENCE_CONFLICT"]:
        triggers.append("R2")
    routing = spec["routing"]
    if agreement is None or (
        agreement < float(routing["agreement_threshold"])
        and len(material_keys) >= routing["agreement_min_material_union"]
    ):
        triggers.append("R3")
    unsupported = {
        side: sum(item["side"] == side for item in buckets["UNSUPPORTED"]) for side in ("a", "b")
    }
    if (
        max(unsupported.values()) >= routing["unsupported_threshold"]
        or abs(omissions["a"] - omissions["b"]) >= routing["omission_imbalance_threshold"]
    ):
        triggers.append("R4")
    return {
        "buckets": buckets,
        "material_union_count": len(material_keys),
        "shared_aligned_material": aligned,
        "omissions": omissions,
        "agreement": agreement,
        "triggers": triggers,
        "routing_decision": "RED_TEAM_REQUIRED" if triggers else "READY_TO_SCORE",
    }


class Comparator:
    def __init__(self, database: ResearchDB):
        self.database = database

    def compare_and_persist(
        self, run_id: str, assessment_id_a: str | None = None, assessment_id_b: str | None = None
    ) -> dict[str, Any]:
        with self.database.connect() as db:
            run = db.execute(
                "SELECT * FROM committee_run WHERE committee_run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            rows = {
                row["role"]: row
                for row in db.execute(
                    "SELECT * FROM model_assessment WHERE committee_run_id=?", (run_id,)
                )
            }
            arow, brow = rows.get("neutral_analyst_a"), rows.get("neutral_analyst_b")
            if arow is None or brow is None:
                raise ValueError("both neutral assessments are required")
            if assessment_id_a and assessment_id_a != arow["assessment_id"]:
                raise ValueError("wrong analyst A assessment")
            if assessment_id_b and assessment_id_b != brow["assessment_id"]:
                raise ValueError("wrong analyst B assessment")
            spec = json.loads(
                db.execute(
                    "SELECT spec_json FROM comparator_definition WHERE config_hash=?",
                    (run["comparator_config_hash"],),
                ).fetchone()[0]
            )
            report = compare_assessments(
                {"claims": json.loads(arow["claims_json"])},
                {"claims": json.loads(brow["claims_json"])},
                spec,
            )
            result_hash = hashlib.sha256(
                ("comparison-result-v1\0" + canonical_json(report)).encode()
            ).hexdigest()
            identity = [
                run_id,
                arow["assessment_id"],
                brow["assessment_id"],
                run["comparator_config_hash"],
            ]
            comparison_id = hashlib.sha256(
                ("comparison-report-v1\0" + canonical_json(identity)).encode()
            ).hexdigest()
            existing = db.execute(
                "SELECT report_json,agreement,routing_decision,result_hash FROM comparison_report WHERE comparison_id=?",
                (comparison_id,),
            ).fetchone()
            expected = (
                canonical_json(report["buckets"]),
                report["agreement"],
                report["routing_decision"],
                result_hash,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise DeterminismError("comparison identity collision")
            else:
                db.execute(
                    "INSERT INTO comparison_report VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (comparison_id, *identity, *expected, utc_now()),
                )
        return {"comparison_id": comparison_id, **report, "result_hash": result_hash}

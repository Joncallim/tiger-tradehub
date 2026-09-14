"""Build the immutable, point-in-time evidence pack v1.

Pack v1 is the **legacy scoring pack**: it was simultaneously the scorer's input
and the committee's context, which is why an unbounded momentum lineage could
block a candidate entirely (#67).  New committee work uses
``committee.lineage.ScoringLineageBuilder`` (scoring) plus
``committee.view.CommitteeViewBuilder`` (bounded model view).  This module is
retained unchanged-in-output so historical packs remain reproducible and
verifiable, and it still serves as the pack for acceptance fixtures.
"""

from __future__ import annotations

# ruff: noqa: E501 -- long SQL projections mirror immutable row layouts.
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tradehub_research.committee.bounds import (
    MAX_BODY_BYTES,
    MAX_EVIDENCE_ROWS,
    MAX_FEATURE_SOURCES,
    MAX_STRING_CODEPOINTS,
    MAX_STRUCTURED_BYTES,
    MAX_STRUCTURED_KEYS,
    TRUNCATION_SUFFIX,
    bound_feature,
    bound_structured,
    hash_prefixed,
    truncate_strings,
)
from tradehub_research.committee.frozen_inputs import (
    FrozenInputs,
    PackBuildError,
    compute_groups,
    identity_body,
    load_frozen_inputs,
)
from tradehub_research.db import ResearchDB, normalize_ts, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

PACK_SPEC_VERSION = 1

# Historical private names, kept importable for callers that referenced them.
_hash = hash_prefixed
_truncate_strings = truncate_strings
_bound_structured = bound_structured
_bound_feature = bound_feature
_groups = compute_groups


@dataclass(frozen=True)
class EvidencePack:
    pack_hash: str
    body: dict[str, Any]

    @property
    def body_json(self) -> str:
        return canonical_json(self.body)


def _freshness_days(as_of: str, public_available_time: str) -> int:
    return max(
        0,
        (
            datetime.fromisoformat(normalize_ts(as_of).replace("Z", "+00:00"))
            - datetime.fromisoformat(normalize_ts(public_available_time).replace("Z", "+00:00"))
        ).days,
    )


class EvidencePackBuilder:
    def __init__(self, database: ResearchDB):
        self.database = database

    def build(self, candidate_id: str) -> EvidencePack:
        # BEGIN pins all reads to one SQLite snapshot, including clusters.
        with self.database.connect() as db:
            db.execute("BEGIN")
            existing = db.execute(
                "SELECT pack_hash,body_json,body_chars FROM evidence_pack "
                "WHERE candidate_id=? AND pack_spec_version=?",
                (candidate_id, PACK_SPEC_VERSION),
            ).fetchone()
            if existing is not None:
                body_json = existing["body_json"]
                try:
                    body = json.loads(body_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise DeterminismError("stored evidence pack is malformed") from exc
                if (
                    len(body_json) != existing["body_chars"]
                    or _hash("evidence-pack-v1", body) != existing["pack_hash"]
                    or body.get("candidate", {}).get("candidate_id") != candidate_id
                ):
                    raise DeterminismError("stored evidence pack failed immutable verification")
                return EvidencePack(existing["pack_hash"], body)
            pack = self._build(db, candidate_id)
            self._persist(db, candidate_id, pack)
            return pack

    build_and_persist = build

    def _build(self, db: sqlite3.Connection, candidate_id: str) -> EvidencePack:
        inputs = load_frozen_inputs(db, candidate_id)
        candidate = inputs.candidate
        ordered_ids = sorted(
            inputs.frozen_ids, key=lambda item: (item not in inputs.passing_ids, item)
        )
        truncations: list[dict[str, Any]] = []
        if len(inputs.passing_ids) > MAX_EVIDENCE_ROWS:
            raise PackBuildError("PACK_TOO_LARGE")
        if len(ordered_ids) > MAX_EVIDENCE_ROWS:
            truncations.append(
                {"kind": "evidence_rows", "omitted": len(ordered_ids) - MAX_EVIDENCE_ROWS}
            )
            ordered_ids = ordered_ids[:MAX_EVIDENCE_ROWS]
        selected = set(ordered_ids)
        evidence_rows: dict[str, Any] = {item: inputs.evidence_rows[item] for item in ordered_ids}
        clusters: dict[str, list[str]] = {item: inputs.clusters[item] for item in ordered_ids}
        groups = self._groups(evidence_rows, clusters, set(inputs.passing_ids))
        successors = {
            row["supersedes_evidence_id"]: evidence_id
            for evidence_id, row in evidence_rows.items()
            if row["supersedes_evidence_id"] in selected
        }
        evidence = []
        for evidence_id in sorted(selected):
            row = evidence_rows[evidence_id]
            fields = _bound_structured(
                json.loads(row["structured_fields"]), evidence_id, truncations
            )
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "source_id": row["source_id"],
                    "source_type": row["source_type"],
                    "hierarchy_tier": row["hierarchy_tier"],
                    "record_type": fields.get("record_type"),
                    "structured_fields": fields,
                    "event_time": row["event_time"],
                    "public_available_time": row["public_available_time"],
                    "pat_provenance": row["pat_provenance"],
                    "extraction_confidence": row["extraction_confidence"],
                    "content_hash": row["content_hash"],
                    "supersedes_evidence_id": row["supersedes_evidence_id"],
                    "superseded_within_pack_by": successors.get(evidence_id),
                    "cluster_ids": clusters[evidence_id],
                    "underlying_group": groups[evidence_id],
                    "freshness_days": _freshness_days(
                        candidate["as_of"], row["public_available_time"]
                    ),
                }
            )
        screens = []
        for item in inputs.results:
            row, evidence_ids, spec = item.row, item.evidence_ids, item.spec
            screens.append(
                {
                    "family": row["family"],
                    "screen_id": row["screen_id"],
                    "screen_version": row["screen_version"],
                    "feature_schema_version": spec["feature_schema_version"],
                    "config_hash": row["config_hash"],
                    "parameters": spec["parameters"],
                    "screen_result_id": row["screen_result_id"],
                    "result_hash": row["result_hash"],
                    "sufficient_data": bool(row["sufficient_data"]),
                    "passed": bool(row["passed"]),
                    "confidence": row["confidence"],
                    "data_quality": row["data_quality"],
                    "reason_codes": sorted(set(json.loads(row["reason_codes_json"]))),
                    "evidence_ids": [entry for entry in evidence_ids if entry in selected],
                    "raw_features": _bound_feature(
                        json.loads(row["raw_features_json"]),
                        f"screens/{row['screen_result_id']}/raw_features",
                        truncations,
                    ),
                }
            )
        screens.sort(
            key=lambda entry: (entry["family"], entry["screen_id"], entry["screen_version"])
        )
        body: dict[str, Any] = {
            "pack_spec_version": PACK_SPEC_VERSION,
            "candidate": {"candidate_id": candidate_id, "security_id": candidate["security_id"]},
            "run": inputs.run_body(),
            "identity": identity_body(inputs, db),
            "screens": screens,
            "evidence": evidence,
            "bounds": {"evidence_rows": len(evidence), "body_chars": 0, "truncations": truncations},
        }
        # body_chars includes its own decimal representation; converge to the fixed point.
        for _ in range(8):
            size = len(canonical_json(body))
            if body["bounds"]["body_chars"] == size:
                break
            body["bounds"]["body_chars"] = size
        encoded = canonical_json(body).encode()
        if len(encoded) > MAX_BODY_BYTES:
            raise PackBuildError("PACK_TOO_LARGE")
        return EvidencePack(_hash("evidence-pack-v1", body), body)

    @staticmethod
    def _groups(
        rows: dict[str, Any], clusters: dict[str, list[str]], passing: set[str] | frozenset[str]
    ) -> dict[str, str]:
        return compute_groups(rows, clusters, passing)

    def _persist(self, db: sqlite3.Connection, candidate_id: str, pack: EvidencePack) -> None:
        body_json = pack.body_json
        expected = (
            pack.pack_hash,
            PACK_SPEC_VERSION,
            candidate_id,
            pack.body["run"]["run_id"],
            body_json,
            len(body_json),
        )
        row = db.execute(
            "SELECT pack_hash,pack_spec_version,candidate_id,pipeline_run_id,body_json,body_chars FROM evidence_pack WHERE candidate_id=? AND pack_spec_version=?",
            (candidate_id, PACK_SPEC_VERSION),
        ).fetchone()
        if row is not None:
            if tuple(row) != expected:
                raise DeterminismError("stored evidence pack differs from deterministic retry")
            return
        db.execute(
            "INSERT INTO evidence_pack(pack_hash,pack_spec_version,candidate_id,pipeline_run_id,body_json,body_chars,built_at) VALUES (?,?,?,?,?,?,?)",
            (*expected, utc_now()),
        )


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_EVIDENCE_ROWS",
    "MAX_FEATURE_SOURCES",
    "MAX_STRING_CODEPOINTS",
    "MAX_STRUCTURED_BYTES",
    "MAX_STRUCTURED_KEYS",
    "PACK_SPEC_VERSION",
    "TRUNCATION_SUFFIX",
    "EvidencePack",
    "EvidencePackBuilder",
    "FrozenInputs",
    "PackBuildError",
]

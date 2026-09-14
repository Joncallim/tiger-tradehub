"""The complete deterministic scoring lineage (#67).

The lineage is the **scoring input of record**.  For one frozen candidate it
carries:

* every frozen screen result with its COMPLETE declared evidence identity
  (no 256-row cap -- the cap that blocked 8/34 genuine candidates);
* the *scoring identity projection* of each screen's raw features
  (``SCORING_PROJECTION_VERSION`` 1, byte-identical to what pack v1 hashed, so
  methodology-change detection and trajectory semantics are unchanged);
* complete evidence identity for every frozen observation.

It deliberately does NOT carry the interpretive evidence payload
(``structured_fields``) or model-facing aggregates: those are the bounded
committee view's job and are not read by ``score_screens()``.

Invariant (#67 §8): model-view size limits must have ZERO effect on the content
of this artifact.  Nothing here is derived from ``bounds.MAX_BODY_BYTES`` or
``bounds.MAX_VIEW_EVIDENCE_ROWS``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tradehub_research.committee.bounds import (
    SCORING_PROJECTION_VERSION,
    SEMANTIC_EVIDENCE_PROJECTION_VERSION,
    bound_feature,
    count_series_observations,
    hash_prefixed,
)
from tradehub_research.committee.frozen_inputs import (
    FrozenInputs,
    PackBuildError,
    identity_body,
    load_frozen_inputs,
)
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

LINEAGE_SPEC_VERSION = 1


@dataclass(frozen=True)
class ScoringLineage:
    lineage_hash: str
    body: dict[str, Any]

    @property
    def body_json(self) -> str:
        return canonical_json(self.body)

    @property
    def screens(self) -> list[dict[str, Any]]:
        return self.body["screens"]

    @property
    def evidence_identity(self) -> list[dict[str, Any]]:
        return self.body["evidence_identity"]


def project_scoring_raw_features(raw_features: Any, path: str) -> tuple[Any, dict[str, Any]]:
    """Reproduce the v1 scoring-identity projection of one screen's features.

    The bounding applied here is scoring semantics (versioned), not a
    model-context limit: the v1 semantic screen hash was computed over exactly
    this projection, so keeping it is what preserves trajectory identity.
    """
    records: list[dict[str, Any]] = []
    projected = bound_feature(raw_features, path, records)
    omitted = sum(
        int(item.get("omitted", 0)) for item in records if item["kind"] == "feature_sources"
    )
    return projected, {
        "projection_version": SCORING_PROJECTION_VERSION,
        "source_refs_total": count_series_observations(raw_features),
        "source_refs_omitted": omitted,
        "string_truncations": sum(1 for item in records if item["kind"] == "string"),
    }


class ScoringLineageBuilder:
    def __init__(self, database: ResearchDB):
        self.database = database

    def build(self, candidate_id: str) -> ScoringLineage:
        with self.database.connect() as db:
            db.execute("BEGIN")
            existing = db.execute(
                "SELECT lineage_hash,body_json,body_chars FROM scoring_lineage "
                "WHERE candidate_id=? AND lineage_spec_version=?",
                (candidate_id, LINEAGE_SPEC_VERSION),
            ).fetchone()
            if existing is not None:
                body_json = existing["body_json"]
                try:
                    body = json.loads(body_json)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise DeterminismError("stored scoring lineage is malformed") from exc
                if (
                    len(body_json) != existing["body_chars"]
                    or hash_prefixed("scoring-lineage-v1", body) != existing["lineage_hash"]
                    or body.get("candidate", {}).get("candidate_id") != candidate_id
                ):
                    raise DeterminismError("stored scoring lineage failed immutable verification")
                return ScoringLineage(existing["lineage_hash"], body)
            inputs = load_frozen_inputs(db, candidate_id)
            lineage = self._build(db, inputs)
            self._persist(db, candidate_id, lineage)
            return lineage

    build_or_reuse = build

    def _build(self, db: Any, inputs: FrozenInputs) -> ScoringLineage:
        screens: list[dict[str, Any]] = []
        for item in inputs.results:
            row = item.row
            spec = item.spec
            raw_features, projection = project_scoring_raw_features(
                json.loads(row["raw_features_json"]),
                f"screens/{row['screen_result_id']}/raw_features",
            )
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
                    "evidence_ids": list(item.evidence_ids),
                    "evidence_id_count": len(item.evidence_ids),
                    "raw_features": raw_features,
                    "raw_features_projection": projection,
                }
            )
        screens.sort(
            key=lambda entry: (entry["family"], entry["screen_id"], entry["screen_version"])
        )
        evidence_identity = inputs.evidence_records()
        body: dict[str, Any] = {
            "lineage_spec_version": LINEAGE_SPEC_VERSION,
            "scoring_projection_version": SCORING_PROJECTION_VERSION,
            "semantic_evidence_projection_version": SEMANTIC_EVIDENCE_PROJECTION_VERSION,
            "candidate": {"candidate_id": inputs.candidate_id, "security_id": inputs.security_id},
            "run": inputs.run_body(),
            "identity": identity_body(inputs, db),
            "screens": screens,
            "evidence_identity": evidence_identity,
            "completeness": {
                "screens": "complete",
                "screen_evidence_ids": "complete",
                "evidence_identity": "complete",
                "interpretive_structured_fields": "excluded: not read by score_screens()",
                "raw_features": (
                    "scoring-identity-projection-v"
                    f"{SCORING_PROJECTION_VERSION}: per-observation evidence ids remain "
                    "complete in evidence_identity and in each screen's evidence_ids"
                ),
                "point_in_time": "candidate.as_of enforced on every evidence row",
            },
            "counts": {
                "screens": len(screens),
                "evidence_identity": len(evidence_identity),
                "frozen_evidence": len(inputs.frozen_ids),
                "passing_evidence": len(inputs.passing_ids),
            },
            "bounds": {"body_chars": 0},
        }
        for _ in range(8):
            size = len(canonical_json(body))
            if body["bounds"]["body_chars"] == size:
                break
            body["bounds"]["body_chars"] = size
        return ScoringLineage(hash_prefixed("scoring-lineage-v1", body), body)

    def _persist(self, db: Any, candidate_id: str, lineage: ScoringLineage) -> None:
        body_json = lineage.body_json
        expected = (
            lineage.lineage_hash,
            LINEAGE_SPEC_VERSION,
            candidate_id,
            lineage.body["run"]["run_id"],
            body_json,
            len(body_json),
        )
        row = db.execute(
            "SELECT lineage_hash,lineage_spec_version,candidate_id,pipeline_run_id,"
            "body_json,body_chars "
            "FROM scoring_lineage WHERE candidate_id=? AND lineage_spec_version=?",
            (candidate_id, LINEAGE_SPEC_VERSION),
        ).fetchone()
        if row is not None:
            if tuple(row) != expected:
                raise DeterminismError("stored scoring lineage differs from deterministic retry")
            return
        db.execute(
            "INSERT INTO scoring_lineage(lineage_hash,lineage_spec_version,candidate_id,"
            "pipeline_run_id,body_json,body_chars,built_at) VALUES (?,?,?,?,?,?,?)",
            (*expected, utc_now()),
        )


__all__ = [
    "LINEAGE_SPEC_VERSION",
    "PackBuildError",
    "ScoringLineage",
    "ScoringLineageBuilder",
    "project_scoring_raw_features",
]

"""Exact Decimal committee-gated Phase-1 scoring and trajectory snapshots."""

# ruff: noqa: E501 -- SQL projections mirror immutable row layouts.

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

Q = Decimal("0.000001")
BASE_FAMILIES = {"valuation", "inflection", "quality", "informed_activity", "event"}


def _d(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Q, rounding=ROUND_HALF_UP)


def _six(value: Decimal) -> Decimal:
    return value.quantize(Q, rounding=ROUND_HALF_UP)


def _independence_units(evidence: list[dict[str, Any]]) -> list[str]:
    """Return deterministic v1 source components linked by shared evidence clusters."""
    source_clusters: dict[str, set[str]] = {}
    cluster_sources: dict[str, set[str]] = {}
    for item in evidence:
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            continue
        clusters = {
            cluster_id
            for cluster_id in item.get("cluster_ids", [])
            if isinstance(cluster_id, str) and cluster_id
        }
        source_clusters.setdefault(source_id, set()).update(clusters)
        for cluster_id in clusters:
            cluster_sources.setdefault(cluster_id, set()).add(source_id)

    neighbors = {source_id: set() for source_id in source_clusters}
    for sources in cluster_sources.values():
        for source_id in sources:
            neighbors[source_id].update(sources - {source_id})

    units = []
    unseen = set(neighbors)
    while unseen:
        root = min(unseen)
        component = {root}
        queue = [root]
        unseen.remove(root)
        while queue:
            current = queue.pop()
            for other in sorted(neighbors[current] & unseen):
                unseen.remove(other)
                component.add(other)
                queue.append(other)
        units.append("independence:v1:" + canonical_json(sorted(component)))
    return sorted(units)


def semantic_screen_payload(screen: dict[str, Any]) -> dict[str, Any]:
    """Project a packed screen onto stable methodology and output semantics."""
    return {
        "family": screen["family"],
        "screen_id": screen["screen_id"],
        "screen_version": screen["screen_version"],
        "feature_schema_version": screen["feature_schema_version"],
        "config_hash": screen["config_hash"],
        "sufficient_data": bool(screen["sufficient_data"]),
        "passed": bool(screen["passed"]),
        "confidence": screen["confidence"],
        "data_quality": screen["data_quality"],
        "reason_codes": sorted(set(screen.get("reason_codes", []))),
        "evidence_ids": sorted(set(screen.get("evidence_ids", []))),
        "raw_features": screen["raw_features"],
    }


def semantic_screen_hash(screen: dict[str, Any]) -> str:
    return _hash("semantic-screen-v1", semantic_screen_payload(screen))


def _semantic_screen_hashes(screens: list[dict[str, Any]]) -> list[str]:
    return sorted(semantic_screen_hash(screen) for screen in screens)


def score_screens(
    screens: list[dict[str, Any]], evidence: list[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any]:
    """Apply §6 exactly. Committee/model fields are deliberately absent."""
    weights = {key: Decimal(str(value)) for key, value in spec["weights"].items()}
    scored_families = [row["family"] for row in screens if row["family"] in weights]
    if len(scored_families) != len(set(scored_families)):
        raise ValueError("duplicate scored screen family")
    by_family = {row["family"]: row for row in screens if row["family"] in weights}
    evidence_by_id = {row["evidence_id"]: row for row in evidence}
    contributions: dict[str, dict[str, Any]] = {}
    base = low_quality = Decimal(0)
    quality_numerator = Decimal(0)
    missing_count = stale_count = 0
    scored_evidence: dict[str, dict[str, Any]] = {}
    reason_codes: set[str] = set()
    for family, weight in weights.items():
        row = by_family.get(family)
        sufficient = bool(row and row["sufficient_data"])
        passed = bool(row and row["passed"])
        quality = _d(row["data_quality"] if row and sufficient else 0)
        family_base = weight if passed else Decimal(0)
        penalty = _six(weight * (Decimal(1) - quality)) if passed else Decimal(0)
        base += family_base
        low_quality += penalty
        quality_numerator += weight * quality
        reasons = list(row.get("reason_codes", [])) if row else []
        reason_codes.update(reasons)
        if family in BASE_FAMILIES and not sufficient:
            missing_count += 1
        if passed and any(str(code).startswith("stale_") for code in reasons):
            stale_count += 1
        if passed:
            for evidence_id in row.get("evidence_ids", []):
                item = evidence_by_id.get(evidence_id)
                if item is None:
                    continue
                group = item.get("underlying_group")
                if not group and item.get("record_type") == "xbrl_fact":
                    raise ValueError("UNGROUPABLE_XBRL")
                scored_evidence[evidence_id] = item
        contributions[family] = {
            "weight": int(weight),
            "passed": passed,
            "sufficient_data": sufficient,
            "data_quality": float(quality),
            "base": float(_six(family_base)),
            "quality_penalty": float(penalty),
        }
    missing_penalty = min(Decimal(10), Decimal(2 * missing_count))
    staleness_penalty = min(Decimal(6), Decimal(2 * stale_count))
    independence_units = _independence_units(list(scored_evidence.values()))
    confluence = Decimal(5 * min(2, max(0, len(independence_units) - 1)))
    raw = _six(
        max(
            Decimal(0),
            min(
                Decimal(100), base + confluence - low_quality - missing_penalty - staleness_penalty
            ),
        )
    )
    conviction = int((raw / Decimal(5)).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 5)
    data_quality = _six(quality_numerator / sum(weights.values()))
    records = [
        {
            "evidence_id": item["evidence_id"],
            "content_hash": item["content_hash"],
            "source_id": item.get("source_id"),
            "underlying_group": item.get("underlying_group"),
            "cluster_ids": sorted(
                {
                    cluster_id
                    for cluster_id in item.get("cluster_ids", [])
                    if isinstance(cluster_id, str) and cluster_id
                }
            ),
            "public_available_time": item["public_available_time"],
            "supersedes_evidence_id": item.get("supersedes_evidence_id"),
        }
        for item in sorted(scored_evidence.values(), key=lambda value: value["evidence_id"])
    ]
    scored_identity = {"records": records, "independence_units": independence_units}
    scored_hash = hashlib.sha256(
        ("scored-evidence-v2\0" + canonical_json(scored_identity)).encode()
    ).hexdigest()
    return {
        "family_contributions": contributions,
        "underlying_groups": independence_units,
        "penalties": {
            "low_quality": float(_six(low_quality)),
            "missing": float(_six(missing_penalty)),
            "staleness": float(_six(staleness_penalty)),
        },
        "base_evidence": float(_six(base)),
        "confluence_bonus": float(_six(confluence)),
        "raw_score": float(raw),
        "conviction": conviction,
        "data_quality": float(data_quality),
        "reason_codes": sorted(reason_codes),
        "scored_evidence": records,
        "scored_evidence_hash": scored_hash,
    }


def _hash(prefix: str, value: object) -> str:
    return hashlib.sha256((prefix + "\0" + canonical_json(value)).encode()).hexdigest()


def classify_trajectory(
    prior: dict[str, Any] | None,
    current: dict[str, Any],
    *,
    screen_hashes_equal: bool,
    committee_hashes_differ: bool,
    correction_chain: bool,
) -> dict[str, Any]:
    """Classify the four mutually exclusive §7 trajectory causes."""
    if prior is None:
        return {"change_cause": "INITIAL", "trajectory_label": "INITIAL", "delta": None}
    if prior["scoring_config_hash"] != current["scoring_config_hash"]:
        return {
            "change_cause": "SCORING_VERSION_CHANGE",
            "trajectory_label": "REBASED",
            "delta": None,
        }
    evidence_equal = prior["scored_evidence_hash"] == current["scored_evidence_hash"]
    if evidence_equal and not screen_hashes_equal:
        return {
            "change_cause": "SCREEN_METHODOLOGY_CHANGE",
            "trajectory_label": "REBASED",
            "delta": None,
        }
    if evidence_equal and screen_hashes_equal and committee_hashes_differ:
        if prior["conviction"] != current["conviction"]:
            raise DeterminismError("model reassessment changed conviction")
        return {"change_cause": "MODEL_REASSESSMENT", "trajectory_label": "STABLE", "delta": 0}
    delta = current["conviction"] - prior["conviction"]
    label = "RISING" if delta > 0 else ("FALLING" if delta < 0 else "STABLE")
    return {
        "change_cause": "CORRECTION_RESTATEMENT" if correction_chain else "EVIDENCE_DRIVEN",
        "trajectory_label": label,
        "delta": delta,
    }


class Scorer:
    def __init__(self, database: ResearchDB):
        self.database = database

    def create_snapshot(self, run_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            run = db.execute(
                "SELECT r.*,c.security_id,p.as_of FROM committee_run r JOIN candidate c ON c.candidate_id=r.candidate_id JOIN pipeline_run p ON p.run_id=r.pipeline_run_id WHERE r.committee_run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            existing_run = db.execute(
                "SELECT * FROM score_snapshot WHERE committee_run_id=? ORDER BY snapshot_id LIMIT 1",
                (run_id,),
            ).fetchone()
            if existing_run is not None:
                if self._state(db, run_id) == "READY_TO_SCORE":
                    self._transition(
                        db,
                        run_id,
                        "READY_TO_SCORE",
                        "SCORED",
                        "SCORE_SNAPSHOT_CREATED",
                        existing_run["snapshot_id"],
                    )
                return self._decode(existing_run)
            state = self._state(db, run_id)
            if state != "READY_TO_SCORE":
                raise ValueError("committee is not ready to score")
            pack = json.loads(
                db.execute(
                    "SELECT body_json FROM evidence_pack WHERE pack_hash=?", (run["pack_hash"],)
                ).fetchone()[0]
            )
            spec = json.loads(
                db.execute(
                    "SELECT spec_json FROM scoring_version WHERE config_hash=?",
                    (run["scoring_config_hash"],),
                ).fetchone()[0]
            )
            comparison = db.execute(
                "SELECT * FROM comparison_report WHERE committee_run_id=? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if comparison is None:
                raise ValueError("comparison required")
            assessments = list(
                db.execute(
                    "SELECT role,assessment_id,semantic_assessment_hash FROM model_assessment "
                    "WHERE committee_run_id=? ORDER BY role",
                    (run_id,),
                )
            )
            resolutions = list(
                db.execute(
                    "SELECT role,resolution_id,focus_hash,result_hash FROM dispute_resolution "
                    "WHERE committee_run_id=? ORDER BY role",
                    (run_id,),
                )
            )
            result = score_screens(pack["screens"], pack["evidence"], spec)
            screen_hashes = _semantic_screen_hashes(pack["screens"])
            logical = {
                "scoring_config_hash": run["scoring_config_hash"],
                "candidate_id": run["candidate_id"],
                "screen_results": screen_hashes,
                "scored_evidence_hash": result["scored_evidence_hash"],
                "comparison": [run["comparator_config_hash"], comparison["result_hash"]],
                "resolutions": [
                    (row["role"], row["focus_hash"], row["result_hash"]) for row in resolutions
                ],
                "assessments": [
                    (row["role"], row["semantic_assessment_hash"]) for row in assessments
                ],
            }
            score_input_hash = _hash("score-input-v1", logical)
            snapshot_id = hashlib.sha256(
                ("score-snapshot-v1\0" + score_input_hash).encode()
            ).hexdigest()
            existing = db.execute(
                "SELECT * FROM score_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if existing is not None:
                self._transition(
                    db,
                    run_id,
                    "READY_TO_SCORE",
                    "SCORED",
                    "SCORE_SNAPSHOT_REUSED",
                    snapshot_id,
                )
                return self._decode(existing)
            prior = db.execute(
                "SELECT s.*,p.as_of FROM score_snapshot s "
                "JOIN candidate c ON c.candidate_id=s.candidate_id "
                "JOIN committee_run previous_run "
                "ON previous_run.committee_run_id=s.committee_run_id "
                "JOIN pipeline_run p ON p.run_id=previous_run.pipeline_run_id "
                "WHERE c.security_id=? AND p.as_of<=? "
                "ORDER BY p.as_of DESC,s.snapshot_id DESC LIMIT 1",
                (run["security_id"], run["as_of"]),
            ).fetchone()
            cause, label, delta, material_time = self._trajectory(
                db, prior, run, result, screen_hashes, logical
            )
            payload = {
                "snapshot_id": snapshot_id,
                "candidate_id": run["candidate_id"],
                "committee_run_id": run_id,
                "scoring_config_hash": run["scoring_config_hash"],
                "score_input_hash": score_input_hash,
                "scored_evidence_hash": result["scored_evidence_hash"],
                "assessment_ids": [row["assessment_id"] for row in assessments],
                "comparison_id": comparison["comparison_id"],
                "resolution_ids": [row["resolution_id"] for row in resolutions],
                **{
                    key: result[key]
                    for key in (
                        "family_contributions",
                        "underlying_groups",
                        "penalties",
                        "base_evidence",
                        "confluence_bonus",
                        "raw_score",
                        "conviction",
                        "data_quality",
                        "reason_codes",
                    )
                },
                "committee_agreement": comparison["agreement"],
                "prior_snapshot_id": prior["snapshot_id"] if prior else None,
                "prior_conviction": prior["conviction"] if prior else None,
                "conviction_delta": delta,
                "trajectory_label": label,
                "change_cause": cause,
                "material_change_time": material_time,
            }
            result_hash = _hash("score-result-v1", payload)
            db.execute(
                "INSERT INTO score_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    run["candidate_id"],
                    run_id,
                    run["scoring_config_hash"],
                    score_input_hash,
                    result["scored_evidence_hash"],
                    canonical_json(payload["assessment_ids"]),
                    comparison["comparison_id"],
                    canonical_json(payload["resolution_ids"]),
                    canonical_json(result["family_contributions"]),
                    canonical_json(result["underlying_groups"]),
                    canonical_json(result["penalties"]),
                    result["base_evidence"],
                    result["confluence_bonus"],
                    result["raw_score"],
                    result["conviction"],
                    result["data_quality"],
                    comparison["agreement"],
                    payload["prior_snapshot_id"],
                    payload["prior_conviction"],
                    delta,
                    label,
                    cause,
                    material_time,
                    canonical_json(result["reason_codes"]),
                    result_hash,
                    utc_now(),
                ),
            )
            self._transition(
                db, run_id, "READY_TO_SCORE", "SCORED", "SCORE_SNAPSHOT_CREATED", snapshot_id
            )
            return {**payload, "result_hash": result_hash}

    @staticmethod
    def _state(db: Any, run_id: str) -> str | None:
        row = db.execute(
            "SELECT to_state FROM committee_transition WHERE committee_run_id=? ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _transition(db: Any, run_id: str, old: str, new: str, cause: str, artifact: str) -> None:
        identity = {
            "committee_run_id": run_id,
            "from_state": old,
            "to_state": new,
            "cause_code": cause,
            "artifact_id": artifact,
        }
        db.execute(
            "INSERT OR IGNORE INTO committee_transition VALUES (?,?,?,?,?,?,?)",
            (
                _hash("committee-transition-v1", identity),
                run_id,
                old,
                new,
                cause,
                artifact,
                utc_now(),
            ),
        )

    def _trajectory(
        self,
        db: Any,
        prior: Any,
        run: Any,
        result: dict[str, Any],
        screen_hashes: list[Any],
        logical: dict[str, Any],
    ) -> tuple[str, str, int | None, str | None]:
        if prior is None:
            return (
                "INITIAL",
                "INITIAL",
                None,
                max(
                    (row["public_available_time"] for row in result["scored_evidence"]),
                    default=None,
                ),
            )
        if prior["scoring_config_hash"] != run["scoring_config_hash"]:
            return "SCORING_VERSION_CHANGE", "REBASED", None, None
        old_run = db.execute(
            "SELECT pack_hash FROM committee_run WHERE committee_run_id=?",
            (prior["committee_run_id"],),
        ).fetchone()
        old_pack = json.loads(
            db.execute(
                "SELECT body_json FROM evidence_pack WHERE pack_hash=?", (old_run[0],)
            ).fetchone()[0]
        )
        old_screens = _semantic_screen_hashes(old_pack["screens"])
        if (
            prior["scored_evidence_hash"] == result["scored_evidence_hash"]
            and old_screens != screen_hashes
        ):
            return "SCREEN_METHODOLOGY_CHANGE", "REBASED", None, None
        if (
            prior["scored_evidence_hash"] == result["scored_evidence_hash"]
            and old_screens == screen_hashes
        ):
            if prior["conviction"] != result["conviction"]:
                raise DeterminismError("model reassessment changed conviction")
            return "MODEL_REASSESSMENT", "STABLE", 0, None
        old_ids = {row["evidence_id"] for row in self._scored_records(old_pack)}
        new_ids = {row["evidence_id"] for row in result["scored_evidence"]}
        changed = old_ids ^ new_ids
        correction = bool(changed) and self._all_in_correction_chains(
            db, changed, old_ids | new_ids
        )
        cause = "CORRECTION_RESTATEMENT" if correction else "EVIDENCE_DRIVEN"
        delta = result["conviction"] - prior["conviction"]
        label = "RISING" if delta > 0 else ("FALLING" if delta < 0 else "STABLE")
        material = max(
            (
                row["public_available_time"]
                for row in result["scored_evidence"]
                if row["evidence_id"] in changed
            ),
            default=None,
        )
        return cause, label, delta, material

    @staticmethod
    def _scored_records(pack: dict[str, Any]) -> list[dict[str, Any]]:
        ids = {
            item
            for screen in pack["screens"]
            if screen["passed"]
            and screen["family"]
            in {
                "valuation",
                "inflection",
                "quality",
                "informed_activity",
                "event",
                "momentum_confirmation",
            }
            for item in screen["evidence_ids"]
        }
        return [row for row in pack["evidence"] if row["evidence_id"] in ids]

    @staticmethod
    def _all_in_correction_chains(db: Any, changed: set[str], universe: set[str]) -> bool:
        rows = {
            row["evidence_id"]: row
            for row in db.execute(
                "SELECT evidence_id,supersedes_evidence_id,withdrawn FROM evidence_event"
            )
        }
        for evidence_id in changed:
            row = rows.get(evidence_id)
            linked = bool(row and (row["withdrawn"] or row["supersedes_evidence_id"] in universe))
            linked = linked or any(
                item["supersedes_evidence_id"] == evidence_id and item["evidence_id"] in universe
                for item in rows.values()
            )
            if not linked:
                return False
        return True

    @staticmethod
    def _decode(row: Any) -> dict[str, Any]:
        result = dict(row)
        for key in (
            "assessment_ids_json",
            "resolution_ids_json",
            "family_contributions_json",
            "underlying_groups_json",
            "penalties_json",
            "reason_codes_json",
        ):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        return result

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT * FROM score_snapshot WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise KeyError(snapshot_id)
            return self._decode(row)

    def list_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        with self.database.connect(read_only=True) as db:
            rows = list(
                db.execute(
                    "SELECT s.* FROM score_snapshot s "
                    "JOIN committee_run r ON r.committee_run_id=s.committee_run_id "
                    "JOIN pipeline_run p ON p.run_id=r.pipeline_run_id "
                    "WHERE s.candidate_id=? ORDER BY p.as_of,s.snapshot_id",
                    (candidate_id,),
                )
            )
        decoded = [self._decode(row) for row in rows]
        for index, item in enumerate(decoded):
            for window in (3, 5):
                values = [
                    entry["conviction"] for entry in decoded[max(0, index - window + 1) : index + 1]
                ]
                item[f"trend_{window}"] = None if len(values) < window else values[-1] - values[0]
        return decoded

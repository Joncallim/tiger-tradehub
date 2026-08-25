"""Build the immutable, point-in-time evidence pack v1."""

from __future__ import annotations

# ruff: noqa: E501 -- long SQL projections mirror immutable row layouts.
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

PACK_SPEC_VERSION = 1
MAX_EVIDENCE_ROWS = 256
MAX_FEATURE_SOURCES = 40
MAX_STRING_CODEPOINTS = 512
MAX_STRUCTURED_KEYS = 32
MAX_STRUCTURED_BYTES = 4096
MAX_BODY_BYTES = 160_000
TRUNCATION_SUFFIX = "…[truncated]"


class PackBuildError(RuntimeError):
    """The frozen Phase-1 inputs cannot produce a conforming pack."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EvidencePack:
    pack_hash: str
    body: dict[str, Any]

    @property
    def body_json(self) -> str:
        return canonical_json(self.body)


def _hash(prefix: str, value: object) -> str:
    return hashlib.sha256((prefix + "\0" + canonical_json(value)).encode()).hexdigest()


def _truncate_strings(value: Any, path: str, records: list[dict[str, Any]]) -> Any:
    if isinstance(value, str) and len(value) > MAX_STRING_CODEPOINTS:
        records.append({"kind": "string", "path": path, "original_codepoints": len(value)})
        return value[: MAX_STRING_CODEPOINTS - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
    if isinstance(value, list):
        return [
            _truncate_strings(item, f"{path}/{index}", records) for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: _truncate_strings(value[key], f"{path}/{key}", records) for key in sorted(value)
        }
    return value


def _bound_structured(
    fields: dict[str, Any], evidence_id: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    keys = sorted(fields)
    if len(keys) > MAX_STRUCTURED_KEYS:
        records.append(
            {
                "kind": "structured_keys",
                "evidence_id": evidence_id,
                "omitted": len(keys) - MAX_STRUCTURED_KEYS,
            }
        )
        keys = keys[:MAX_STRUCTURED_KEYS]
    bounded = _truncate_strings(
        {key: fields[key] for key in keys}, f"evidence/{evidence_id}/structured_fields", records
    )
    if len(canonical_json(bounded).encode()) > MAX_STRUCTURED_BYTES:
        raise PackBuildError("PACK_TOO_LARGE")
    return bounded


def _bound_feature(value: Any, path: str, records: list[dict[str, Any]]) -> Any:
    value = _truncate_strings(value, path, records)
    if isinstance(value, dict):
        return {
            key: _bound_feature(item, f"{path}/{key}", records)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list) and path.endswith("/sources"):

        def order(item: Any) -> tuple[str, str]:
            if not isinstance(item, dict):
                return ("", canonical_json(item))
            return (str(item.get("public_available_time", "")), str(item.get("evidence_id", "")))

        ordered = sorted(value, key=order, reverse=True)
        if len(ordered) > MAX_FEATURE_SOURCES:
            records.append(
                {
                    "kind": "feature_sources",
                    "path": path,
                    "omitted": len(ordered) - MAX_FEATURE_SOURCES,
                }
            )
        return ordered[:MAX_FEATURE_SOURCES]
    if isinstance(value, list):
        return [
            _bound_feature(item, f"{path}/{index}", records) for index, item in enumerate(value)
        ]
    return value


class EvidencePackBuilder:
    def __init__(self, database: ResearchDB):
        self.database = database

    def build(self, candidate_id: str) -> EvidencePack:
        # BEGIN pins all reads to one SQLite snapshot, including clusters.
        with self.database.connect() as db:
            db.execute("BEGIN")
            pack = self._build(db, candidate_id)
            self._persist(db, candidate_id, pack)
            return pack

    build_and_persist = build

    def _build(self, db: sqlite3.Connection, candidate_id: str) -> EvidencePack:
        candidate = db.execute(
            "SELECT c.*,p.as_of,p.universe_hash,p.screen_manifest_hash,p.funnel_config_hash,"
            "p.input_view_hash,p.input_snapshot_id,p.flags_json,s.canonical_ticker,s.name,s.sector,"
            "s.sector_coverage_status FROM candidate c JOIN pipeline_run p ON p.run_id=c.run_id "
            "JOIN security s ON s.security_id=c.security_id WHERE c.candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            raise KeyError(f"unknown candidate: {candidate_id}")
        if candidate["is_control"]:
            raise PackBuildError("CONTROL_CANDIDATE")
        result_ids = json.loads(candidate["screen_result_ids_json"])
        if not isinstance(result_ids, list):
            raise PackBuildError("INVALID_SCREEN_RESULTS")
        results = []
        frozen_ids: set[str] = set()
        passing_ids: set[str] = set()
        for result_id in sorted(set(result_ids)):
            row = db.execute(
                "SELECT r.*,d.family,d.screen_id,d.screen_version,d.spec_json FROM screen_result r "
                "JOIN screen_definition d ON d.config_hash=r.config_hash WHERE r.screen_result_id=?",
                (result_id,),
            ).fetchone()
            if (
                row is None
                or row["run_id"] != candidate["run_id"]
                or row["security_id"] != candidate["security_id"]
            ):
                raise PackBuildError("SCREEN_CANDIDATE_MISMATCH")
            evidence_ids = sorted(set(json.loads(row["evidence_ids_json"])))
            frozen_ids.update(evidence_ids)
            if row["passed"]:
                passing_ids.update(evidence_ids)
            spec = json.loads(row["spec_json"])
            results.append((row, evidence_ids, spec))

        ordered_ids = sorted(frozen_ids, key=lambda item: (item not in passing_ids, item))
        truncations: list[dict[str, Any]] = []
        if len(ordered_ids) > MAX_EVIDENCE_ROWS:
            truncations.append(
                {"kind": "evidence_rows", "omitted": len(ordered_ids) - MAX_EVIDENCE_ROWS}
            )
            ordered_ids = ordered_ids[:MAX_EVIDENCE_ROWS]
        selected = set(ordered_ids)
        evidence_rows: dict[str, sqlite3.Row] = {}
        clusters: dict[str, list[str]] = {}
        for evidence_id in ordered_ids:
            row = db.execute(
                "SELECT e.*,s.source_type,s.hierarchy_tier FROM evidence_event e "
                "JOIN evidence_source s ON s.source_id=e.source_id WHERE e.evidence_id=?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise PackBuildError("MISSING_EVIDENCE")
            if row["security_id"] != candidate["security_id"]:
                raise PackBuildError("EVIDENCE_SECURITY_MISMATCH")
            if row["public_available_time"] is None or normalize_ts(
                row["public_available_time"]
            ) > normalize_ts(candidate["as_of"]):
                raise PackBuildError("EVIDENCE_NOT_POINT_IN_TIME")
            if row["pat_provenance"] not in ("source_reported", "derived_from_index"):
                raise PackBuildError("EVIDENCE_PROVENANCE")
            if row["withdrawn"]:
                raise PackBuildError("EVIDENCE_WITHDRAWN")
            evidence_rows[evidence_id] = row
            clusters[evidence_id] = [
                item[0]
                for item in db.execute(
                    "SELECT m.cluster_id FROM evidence_cluster_member m JOIN evidence_cluster c "
                    "ON c.cluster_id=m.cluster_id WHERE m.evidence_id=? AND c.formed_at<=? ORDER BY m.cluster_id",
                    (evidence_id, candidate["as_of"]),
                )
            ]

        groups = self._groups(evidence_rows, clusters, passing_ids)
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
            from datetime import datetime

            freshness = max(
                0,
                (
                    datetime.fromisoformat(normalize_ts(candidate["as_of"]).replace("Z", "+00:00"))
                    - datetime.fromisoformat(
                        normalize_ts(row["public_available_time"]).replace("Z", "+00:00")
                    )
                ).days,
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
                    "freshness_days": freshness,
                }
            )
        screens = []
        for row, evidence_ids, spec in results:
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
                    "evidence_ids": [item for item in evidence_ids if item in selected],
                    "raw_features": _bound_feature(
                        json.loads(row["raw_features_json"]),
                        f"screens/{row['screen_result_id']}/raw_features",
                        truncations,
                    ),
                }
            )
        screens.sort(key=lambda item: (item["family"], item["screen_id"], item["screen_version"]))
        flags = sorted(set(json.loads(candidate["flags_json"] or "[]")))
        ticker_event = db.execute(
            "SELECT new_value FROM security_identity_event WHERE security_id=? "
            "AND event_type='ticker_change' AND public_available_time IS NOT NULL "
            "AND public_available_time<=? AND pat_provenance IN "
            "('source_reported','derived_from_index') ORDER BY event_time DESC,id DESC LIMIT 1",
            (candidate["security_id"], candidate["as_of"]),
        ).fetchone()
        if ticker_event is None:
            ticker_event = db.execute(
                "SELECT old_value FROM security_identity_event WHERE security_id=? "
                "AND event_type='ticker_change' AND public_available_time>? "
                "ORDER BY public_available_time,event_time,id LIMIT 1",
                (candidate["security_id"], candidate["as_of"]),
            ).fetchone()
        ticker_as_of = (
            candidate["canonical_ticker"]
            if ticker_event is None or not ticker_event[0]
            else ticker_event[0]
        )
        body: dict[str, Any] = {
            "pack_spec_version": PACK_SPEC_VERSION,
            "candidate": {"candidate_id": candidate_id, "security_id": candidate["security_id"]},
            "run": {
                "run_id": candidate["run_id"],
                "as_of": candidate["as_of"],
                "universe_hash": candidate["universe_hash"],
                "screen_manifest_hash": candidate["screen_manifest_hash"],
                "funnel_config_hash": candidate["funnel_config_hash"],
                "input_view_hash": candidate["input_view_hash"],
                "input_snapshot_id": candidate["input_snapshot_id"],
                "flags": flags,
            },
            "identity": {
                "ticker_as_of": ticker_as_of,
                "name": candidate["name"],
                "sector": candidate["sector"],
                "sector_coverage_status": candidate["sector_coverage_status"],
            },
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
        rows: dict[str, sqlite3.Row], clusters: dict[str, list[str]], passing: set[str]
    ) -> dict[str, str]:
        groups: dict[str, str] = {}
        non_xbrl = []
        for evidence_id, row in rows.items():
            fields = json.loads(row["structured_fields"])
            if fields.get("record_type") == "xbrl_fact":
                accession = str(fields.get("accession", "")).strip()
                if not accession and evidence_id in passing:
                    raise PackBuildError("UNGROUPABLE_XBRL")
                groups[evidence_id] = (
                    f"xbrl:{row['source_id']}:{accession}"
                    if accession
                    else f"event:{row['source_id']}:{evidence_id}"
                )
            else:
                non_xbrl.append(evidence_id)
        # Connected components are source-local and connected by any shared PIT-valid cluster.
        unseen = set(non_xbrl)
        while unseen:
            root = min(unseen)
            component = {root}
            queue = [root]
            unseen.remove(root)
            while queue:
                current = queue.pop()
                for other in sorted(unseen):
                    if rows[other]["source_id"] == rows[current]["source_id"] and set(
                        clusters[other]
                    ) & set(clusters[current]):
                        unseen.remove(other)
                        component.add(other)
                        queue.append(other)
            all_clusters = sorted({cluster for item in component for cluster in clusters[item]})
            for item in component:
                groups[item] = (
                    f"cluster:{rows[item]['source_id']}:{all_clusters[0]}"
                    if all_clusters
                    else f"event:{rows[item]['source_id']}:{item}"
                )
        return groups

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

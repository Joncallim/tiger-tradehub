"""The single frozen point-in-time read shared by every committee artifact.

Pack v1, the #67 scoring lineage and the #67 bounded committee view all read the
frozen inputs through this module.  Keeping one loader means the PIT firewall,
provenance gates, identity checks and underlying-group derivation cannot drift
between the artifact a scorer consumes and the artifact a model reads.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from tradehub_research.committee.bounds import MAX_EVIDENCE_ROWS
from tradehub_research.db import normalize_ts
from tradehub_research.universe import SecurityIdentityStore

ALLOWED_PAT_PROVENANCE = ("source_reported", "derived_from_index")


class PackBuildError(RuntimeError):
    """The frozen inputs cannot produce a conforming artifact."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ScreenInput:
    """One frozen screen result plus its declared evidence identity."""

    row: Any
    evidence_ids: tuple[str, ...]
    spec: dict[str, Any]

    @property
    def family(self) -> str:
        return self.row["family"]

    @property
    def passed(self) -> bool:
        return bool(self.row["passed"])


@dataclass(frozen=True)
class FrozenInputs:
    candidate: Any
    results: tuple[ScreenInput, ...]
    evidence_rows: dict[str, Any]
    clusters: dict[str, list[str]]
    groups: dict[str, str]
    frozen_ids: tuple[str, ...]
    passing_ids: frozenset[str]

    @property
    def candidate_id(self) -> str:
        return self.candidate["candidate_id"]

    @property
    def security_id(self) -> str:
        return self.candidate["security_id"]

    @property
    def run_id(self) -> str:
        return self.candidate["run_id"]

    @property
    def as_of(self) -> str:
        return self.candidate["as_of"]

    def run_body(self) -> dict[str, Any]:
        return {
            "run_id": self.candidate["run_id"],
            "as_of": self.candidate["as_of"],
            "universe_hash": self.candidate["universe_hash"],
            "screen_manifest_hash": self.candidate["screen_manifest_hash"],
            "funnel_config_hash": self.candidate["funnel_config_hash"],
            "input_view_hash": self.candidate["input_view_hash"],
            "input_snapshot_id": self.candidate["input_snapshot_id"],
            "flags": sorted(set(json.loads(self.candidate["flags_json"] or "[]"))),
        }

    def evidence_records(self) -> list[dict[str, Any]]:
        """Scoring-side evidence identity for every frozen observation, by id."""
        rows = []
        for evidence_id in sorted(self.evidence_rows):
            row = self.evidence_rows[evidence_id]
            fields = self._raw_structured_fields(row)
            rows.append(
                {
                    "evidence_id": evidence_id,
                    "source_id": row["source_id"],
                    "source_type": row["source_type"],
                    "hierarchy_tier": row["hierarchy_tier"],
                    "record_type": fields.get("record_type"),
                    "content_hash": row["content_hash"],
                    "event_time": row["event_time"],
                    "public_available_time": row["public_available_time"],
                    "pat_provenance": row["pat_provenance"],
                    "extraction_confidence": row["extraction_confidence"],
                    "supersedes_evidence_id": row["supersedes_evidence_id"],
                    "cluster_ids": list(self.clusters.get(evidence_id, [])),
                    "underlying_group": self.groups.get(evidence_id),
                }
            )
        return rows

    @staticmethod
    def _raw_structured_fields(row: Any) -> dict[str, Any]:
        try:
            fields = json.loads(row["structured_fields"])
        except (TypeError, json.JSONDecodeError):
            return {}
        return fields if isinstance(fields, dict) else {}


def identity_body(inputs: FrozenInputs, db: sqlite3.Connection) -> dict[str, Any]:
    """Ticker/name/sector identity, resolved as-of for point-in-time honesty."""
    candidate = inputs.candidate
    ticker_as_of = SecurityIdentityStore.ticker_at_connection(
        db, candidate["security_id"], candidate["as_of"]
    )
    if (
        ticker_as_of is None
        and not SecurityIdentityStore.has_authoritative_ticker_history_connection(
            db, candidate["security_id"]
        )
    ):
        ticker_as_of = candidate["canonical_ticker"]
    return {
        "ticker_as_of": ticker_as_of,
        "name": candidate["name"],
        "sector": candidate["sector"],
        "sector_coverage_status": candidate["sector_coverage_status"],
    }


def load_frozen_inputs(db: sqlite3.Connection, candidate_id: str) -> FrozenInputs:
    """Read the frozen inputs for one candidate under the PIT firewall.

    Every gate here is load-bearing for scoring identity: a candidate whose
    frozen evidence cannot be read cleanly fails closed rather than producing a
    partial lineage.
    """
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

    results: list[ScreenInput] = []
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
        evidence_ids = tuple(sorted(set(json.loads(row["evidence_ids_json"]))))
        frozen_ids.update(evidence_ids)
        if row["passed"]:
            passing_ids.update(evidence_ids)
        results.append(
            ScreenInput(row=row, evidence_ids=evidence_ids, spec=json.loads(row["spec_json"]))
        )

    evidence_rows: dict[str, Any] = {}
    clusters: dict[str, list[str]] = {}
    for evidence_id in sorted(frozen_ids):
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
        if row["pat_provenance"] not in ALLOWED_PAT_PROVENANCE:
            raise PackBuildError("EVIDENCE_PROVENANCE")
        if row["withdrawn"]:
            raise PackBuildError("EVIDENCE_WITHDRAWN")
        evidence_rows[evidence_id] = row
        clusters[evidence_id] = [
            item[0]
            for item in db.execute(
                "SELECT m.cluster_id FROM evidence_cluster_member m JOIN evidence_cluster c "
                "ON c.cluster_id=m.cluster_id WHERE m.evidence_id=? AND c.formed_at<=? "
                "ORDER BY m.cluster_id",
                (evidence_id, candidate["as_of"]),
            )
        ]

    return FrozenInputs(
        candidate=candidate,
        results=tuple(results),
        evidence_rows=evidence_rows,
        clusters=clusters,
        groups=scoring_group_labels(
            evidence_rows,
            clusters,
            passing_ids,
            historical_selection(frozen_ids, passing_ids),
        ),
        frozen_ids=tuple(sorted(frozen_ids)),
        passing_ids=frozenset(passing_ids),
    )


def historical_selection(frozen_ids: Any, passing: set[str] | frozenset[str]) -> tuple[str, ...]:
    """The rows the historical (v1) derivation covered.

    Exactly pack v1's selection: the first ``MAX_EVIDENCE_ROWS`` ids ordered
    passing-first, then by id.
    """
    ordered = sorted(frozen_ids, key=lambda item: (item not in passing, item))
    return tuple(ordered[:MAX_EVIDENCE_ROWS])


def scoring_group_labels(
    rows: dict[str, Any],
    clusters: dict[str, list[str]],
    passing: set[str] | frozenset[str],
    selection: tuple[str, ...],
) -> dict[str, str]:
    """Confluence groups anchored to the historical (v1) derivation.

    pack v1 derived ``underlying_group`` over its **selected rows only** (the
    first ``MAX_EVIDENCE_ROWS`` passing-first ids), and that derivation is
    scoring identity: the same frozen inputs must yield the labels the v1 pack
    scored, or a representation change alone would move ``scored_evidence_hash``
    and manufacture ``EVIDENCE_DRIVEN`` (review finding P2, round 4). Because
    component labels are canonicalised over a component's cluster union, looking
    at more rows can merge components and shift the label of an already-scored
    row, so the selection-restricted derivation is authoritative for selected
    rows.

    Rows outside the selection (they exist only for candidates whose v1 pack
    could never be built) are attached deterministically afterwards: to the
    merged component's anchor label when the component contains selected rows,
    otherwise to the full structural label.
    """
    anchor_ids = [item for item in selection if item in rows]
    anchors = compute_groups(
        {item: rows[item] for item in anchor_ids},
        {item: clusters[item] for item in anchor_ids},
        passing & set(anchor_ids),
    )
    labels: dict[str, str] = dict(anchors)
    if len(anchor_ids) == len(rows):
        return labels
    structural = compute_groups(rows, clusters, passing)
    merged: dict[str, set[str]] = {}
    for evidence_id, anchor_label in anchors.items():
        merged.setdefault(structural[evidence_id], set()).add(anchor_label)
    for evidence_id in rows:
        if evidence_id in labels:
            continue
        component = structural[evidence_id]
        component_anchors = merged.get(component)
        labels[evidence_id] = min(component_anchors) if component_anchors else component
    return labels


def compute_groups(
    rows: dict[str, Any], clusters: dict[str, list[str]], passing: set[str] | frozenset[str]
) -> dict[str, str]:
    """Group evidence into underlying units for confluence independence."""
    groups: dict[str, str] = {}
    non_xbrl = []
    for evidence_id, row in rows.items():
        fields = FrozenInputs._raw_structured_fields(row)
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

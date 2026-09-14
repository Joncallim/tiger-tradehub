"""The bounded, model-facing committee view (#67).

This artifact is what a committee model may read.  It is derived from the frozen
inputs plus the scoring lineage -- never the other way round:

* series features (momentum bars and friends) are replaced by a
  **deterministic aggregate computed in code** (value, observation counts,
  session-date range, representative end-point observations, ``lineage_set_hash``)
  instead of serializing thousands of bars into model context.  Observations
  folded into an aggregate are *represented by* it: they are not re-serialized as
  individual rows, so they cannot crowd out genuine interpretive evidence;
* the interpretive evidence payload stays bounded by row cap and byte budget,
  with explicit omission metadata;
* the scoring lineage is referenced by hash, and the artifact states
  structurally that it is an aggregate view.

Honesty contract: a model can cite presented evidence rows only (including the
representative observations the aggregates carry).  It may reason about the
aggregate, but it must never imply it inspected observations that were not
presented.

Model-view limits in this module must have ZERO effect on scoring: nothing here
is read by ``Scorer.create_snapshot`` (which consumes the lineage).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tradehub_research.committee.bounds import (
    MAX_BODY_BYTES,
    MAX_SERIES_REPRESENTATIVES,
    MAX_VIEW_EVIDENCE_ROWS,
    SCORING_PROJECTION_VERSION,
    bound_structured,
    hash_prefixed,
    series_observation_ids,
    truncate_strings,
)
from tradehub_research.committee.frozen_inputs import (
    FrozenInputs,
    PackBuildError,
    identity_body,
    load_frozen_inputs,
)
from tradehub_research.committee.lineage import (
    LINEAGE_SPEC_VERSION,
    ScoringLineage,
    ScoringLineageBuilder,
)
from tradehub_research.db import ResearchDB, normalize_ts, utc_now
from tradehub_research.screen_store import DeterminismError
from tradehub_research.screens import canonical_json

VIEW_SPEC_VERSION = 1
#: ``evidence_pack.pack_spec_version`` value reserved for the committee view.
VIEW_PACK_SPEC_VERSION = 2
REPRESENTATION = "BOUNDED_COMMITTEE_VIEW"
_AGGREGATE = "DETERMINISTIC_AGGREGATE"
_SERIES_OMISSION_SEMANTICS = "representation_compaction_of_frozen_observations_retained_in_lineage"
_INTERPRETIVE_OMISSION_SEMANTICS = "capacity_bound_not_presented_and_not_aggregated"


def _omission_semantics() -> dict[str, str]:
    """Distinct semantics for the two kinds of omission (review finding P3).

    Series observations omitted by aggregation are *represented* by a
    deterministic aggregate and retained complete in the scoring lineage.
    Interpretive rows omitted by the view's capacity bounds are neither
    presented nor aggregated: the model simply has not seen them. Both are
    representation facts, not data-quality signals -- but they must not share a
    label that could imply nothing was lost when something was.
    """
    return {
        "series_observations": _SERIES_OMISSION_SEMANTICS,
        "series_references_outside_frozen_set": (
            "referenced by a feature's series but not part of this candidate's frozen "
            "evidence; these are in no lineage and in no aggregate"
        ),
        "interpretive_rows": _INTERPRETIVE_OMISSION_SEMANTICS,
        "data_quality_signal": (
            "neither omission kind is a data-quality signal; coverage is reported by each "
            "screen's data_quality and sufficient_data"
        ),
    }


@dataclass(frozen=True)
class CommitteeView:
    pack_hash: str
    body: dict[str, Any]
    lineage_hash: str

    @property
    def body_json(self) -> str:
        return canonical_json(self.body)


def _observation_sort_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item.get("session_date") or ""), str(item.get("evidence_id") or ""))


def resolve_aggregate(features: Any, path: str) -> dict[str, Any]:
    """Resolve the *shipped* aggregate object for an aggregate path.

    ``truncate_strings`` rebuilds the feature tree, so the object that ends up in
    ``screens[].raw_features`` is a copy of the one the builder collected. Trim
    must be applied to the copy that actually ships (review finding P1, round 4),
    which is what this locator provides.
    """
    relative = path.split("/raw_features/", 1)[1].split("/")
    cursor: Any = features
    for part in relative:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    return cursor


def _series_aggregate(
    value: dict[str, Any], sources: list[Any], admissible_ids: set[str]
) -> dict[str, Any]:
    """Summarise one observation series deterministically.

    ``observation_count`` and ``lineage_set_hash`` describe the **complete**
    series.  The representative sample is drawn only from observations that
    actually exist as evidence rows for this candidate (review finding P1, round
    3): a screen's declared ``evidence_ids`` is commonly a curated subset of the
    ids referenced by its raw features, so sampling the raw series could
    advertise an id that can never be presented -- and therefore never be cited.
    """
    items = [item for item in sources if isinstance(item, dict)]
    admissible = [
        item for item in items if admissible_ids and str(item.get("evidence_id")) in admissible_ids
    ]
    ordered = sorted(admissible, key=_observation_sort_key)
    identity = sorted(
        canonical_json(
            {key: item.get(key) for key in ("evidence_id", "session_date", "value", "unit", "role")}
        )
        for item in items
    )
    full_sorted = sorted(items, key=_observation_sort_key)
    total = len(items)
    if len(ordered) <= MAX_SERIES_REPRESENTATIVES:
        presented = ordered
    else:
        head = MAX_SERIES_REPRESENTATIVES // 2
        presented = ordered[:head] + ordered[-(MAX_SERIES_REPRESENTATIVES - head) :]
    compacted = len(ordered) - len(presented)
    return {
        "value": value.get("value"),
        "unit": value.get("unit"),
        "representation": _AGGREGATE,
        "observation_count": total,
        "observations_presented": len(presented),
        # Split deliberately (review finding P3, round 4): observations that are
        # frozen evidence but only present in aggregate form are retained in the
        # scoring lineage, while observations referenced by the feature but never
        # part of this candidate's frozen evidence are in no lineage at all.
        "observations_compacted": compacted,
        "observations_not_frozen": total - len(ordered),
        "observations_omitted": total - len(presented),
        "session_date_range": (
            [full_sorted[0].get("session_date"), full_sorted[-1].get("session_date")]
            if full_sorted
            else None
        ),
        "representative_observations": presented,
        "representative_evidence_ids": [
            str(item["evidence_id"]) for item in presented if item.get("evidence_id")
        ],
        "lineage_set_hash": hash_prefixed("series-lineage-v1", identity),
        "citation": (
            "aggregate computed in code from the frozen series; individual observations are "
            "retained in the scoring lineage and are not individually citable in this view"
        ),
    }


def aggregate_series(
    value: Any,
    path: str,
    aggregates: list[dict[str, Any]],
    aggregated_ids: set[str],
    admissible_ids: set[str],
) -> Any:
    """Replace every observation series with a deterministic aggregate.

    Every ``sources`` list is aggregated, regardless of size (review finding P2,
    round 2). A size threshold would leave small series serialized verbatim
    inside ``raw_features``, so their observation ids would be visible to the
    model yet absent from ``body["evidence"]`` -- uncitable, and counted as
    "not presented" by the artifact's own honesty accounting. Aggregating all of
    them makes the two sets agree exactly: every series observation is either a
    published representative row (visible and citable) or an explicitly counted
    aggregate omission (invisible).

    ``aggregates`` collects ``{"path", "aggregate"}`` pairs so the caller can
    trim each aggregate to the observations it actually presented, keeping the
    model-visible copy and the summary copy in agreement.
    """
    if isinstance(value, dict):
        sources = value.get("sources")
        if isinstance(sources, list):
            aggregate = _series_aggregate(value, sources, admissible_ids)
            aggregated_ids.update(series_observation_ids(value))
            aggregates.append({"path": path, "aggregate": aggregate})
            return aggregate
        return {
            key: aggregate_series(
                value[key], f"{path}/{key}", aggregates, aggregated_ids, admissible_ids
            )
            for key in sorted(value)
        }
    if isinstance(value, list):
        return [
            aggregate_series(item, f"{path}/{index}", aggregates, aggregated_ids, admissible_ids)
            for index, item in enumerate(value)
        ]
    return value


def _freshness_days(as_of: str, public_available_time: str | None) -> int | None:
    if public_available_time is None:
        return None
    delta = datetime.fromisoformat(
        normalize_ts(as_of).replace("Z", "+00:00")
    ) - datetime.fromisoformat(normalize_ts(public_available_time).replace("Z", "+00:00"))
    return max(0, delta.days)


class CommitteeViewBuilder:
    """Build the bounded committee view for one candidate."""

    def __init__(self, database: ResearchDB):
        self.database = database

    _STORED_SQL = (
        "SELECT pack_hash,body_json,body_chars FROM evidence_pack "
        "WHERE candidate_id=? AND pack_spec_version=?"
    )

    def build(self, candidate_id: str, lineage: ScoringLineage | None = None) -> CommitteeView:
        # The memo check is a read; the lineage is built in its own transaction.
        # Neither may be nested inside this builder's write transaction or the
        # connections deadlock on the SQLite write lock.
        with self.database.connect(read_only=True) as conn:
            stored = self._verify_stored(
                conn.execute(self._STORED_SQL, (candidate_id, VIEW_PACK_SPEC_VERSION)).fetchone(),
                candidate_id,
            )
        if stored is not None:
            return stored
        if lineage is None:
            lineage = ScoringLineageBuilder(self.database).build(candidate_id)
        elif (lineage.body.get("candidate") or {}).get("candidate_id") != candidate_id:
            # Defence in depth (review finding P3, round 4): the view embeds the
            # lineage by hash and the run mapping trusts that reference, so a
            # foreign lineage must never be accepted silently.
            raise DeterminismError("committee view lineage belongs to a different candidate")
        with self.database.connect() as db:
            db.execute("BEGIN")
            stored = self._verify_stored(
                db.execute(self._STORED_SQL, (candidate_id, VIEW_PACK_SPEC_VERSION)).fetchone(),
                candidate_id,
            )
            if stored is not None:
                return stored
            inputs = load_frozen_inputs(db, candidate_id)
            view = self._build(db, inputs, lineage)
            self._persist(db, candidate_id, view)
            return view

    @staticmethod
    def _verify_stored(existing: Any, candidate_id: str) -> CommitteeView | None:
        if existing is None:
            return None
        body_json = existing["body_json"]
        try:
            body = json.loads(body_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DeterminismError("stored committee view is malformed") from exc
        if (
            len(body_json) != existing["body_chars"]
            or hash_prefixed("committee-view-v1", body) != existing["pack_hash"]
            or body.get("candidate", {}).get("candidate_id") != candidate_id
        ):
            raise DeterminismError("stored committee view failed immutable verification")
        return CommitteeView(existing["pack_hash"], body, body["lineage"]["lineage_hash"])

    build_and_persist = build

    def _build(self, db: Any, inputs: FrozenInputs, lineage: ScoringLineage) -> CommitteeView:
        truncations: list[dict[str, Any]] = []
        screens: list[dict[str, Any]] = []
        screen_aggregates: dict[str, list[dict[str, Any]]] = {}
        aggregated_ids: set[str] = set()
        representative_ids: list[str] = []
        series_references = 0
        series_not_frozen = 0
        admissible_ids = set(inputs.evidence_rows)
        for item in inputs.results:
            row = item.row
            spec = item.spec
            pairs: list[dict[str, Any]] = []
            features = aggregate_series(
                json.loads(row["raw_features_json"]),
                f"screens/{row['screen_result_id']}/raw_features",
                pairs,
                aggregated_ids,
                admissible_ids,
            )
            features = truncate_strings(
                features, f"screens/{row['screen_result_id']}/raw_features", truncations
            )
            evidence_ids = list(item.evidence_ids)
            series_references += sum(pair["aggregate"]["observation_count"] for pair in pairs)
            for pair in pairs:
                for evidence_id in pair["aggregate"]["representative_evidence_ids"]:
                    if evidence_id not in representative_ids:
                        representative_ids.append(evidence_id)
            screen_aggregates[row["screen_result_id"]] = pairs
            if pairs:
                # Series screens present their representative observations; the
                # complete id set is committed by digest instead of being
                # re-serialized (thousands of ids would consume the budget that
                # belongs to interpretive evidence).
                presented_ids = [
                    evidence_id
                    for pair in pairs
                    for evidence_id in pair["aggregate"]["representative_evidence_ids"]
                ][:MAX_VIEW_EVIDENCE_ROWS]
            else:
                presented_ids = evidence_ids[:MAX_VIEW_EVIDENCE_ROWS]
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
                    "evidence_ids": presented_ids,
                    "evidence_id_count": len(evidence_ids),
                    "evidence_ids_omitted": len(evidence_ids) - len(presented_ids),
                    "evidence_id_set_hash": hash_prefixed(
                        "screen-evidence-ids-v1", sorted(evidence_ids)
                    ),
                    "representation": _AGGREGATE if pairs else "BOUNDED",
                    "raw_features": features,
                    # Filled after admission so the summary can only ever list
                    # representatives that really are presented evidence rows.
                    "series_aggregates": [],
                }
            )
        screens.sort(
            key=lambda entry: (entry["family"], entry["screen_id"], entry["screen_version"])
        )

        # Interpretive evidence first: observations that an aggregate already
        # represents must not crowd out evidence the model actually needs.
        interpretive = sorted(
            (item for item in inputs.frozen_ids if item not in aggregated_ids),
            key=lambda item: (item not in inputs.passing_ids, item),
        )
        skeleton: dict[str, Any] = {
            "view_spec_version": VIEW_SPEC_VERSION,
            "representation": REPRESENTATION,
            "candidate": {"candidate_id": inputs.candidate_id, "security_id": inputs.security_id},
            "run": inputs.run_body(),
            "identity": identity_body(inputs, db),
            "lineage": {
                "lineage_spec_version": LINEAGE_SPEC_VERSION,
                "lineage_hash": lineage.lineage_hash,
                "scoring_projection_version": SCORING_PROJECTION_VERSION,
                "full_lineage_available": True,
                "evidence_identity_count": int(lineage.body["counts"]["evidence_identity"]),
                "screen_count": len(lineage.screens),
            },
            "screens": screens,
            "evidence": [],
            "evidence_omitted": {
                "count": 0,
                "reasons": {},
                "semantics": _omission_semantics(),
                "deterministic_order": (
                    "interpretive_passing_first_then_evidence_id, then series representatives"
                ),
            },
            "series_representation": {
                "distinct_observations": len(aggregated_ids),
                "observation_references": series_references,
                "represented_by": "deterministic per-feature aggregates",
                "individual_observations_in_lineage": True,
                "omission_semantics": _SERIES_OMISSION_SEMANTICS,
            },
            "model_honesty": {
                "representation": REPRESENTATION,
                "aggregated_series_present": bool(aggregated_ids),
                "evidence_presented": 0,
                "evidence_omitted": 0,
                "citation_scope": "evidence rows presented in this view",
                "reasoning_scope": (
                    "deterministic aggregates and presented evidence rows; the model must not "
                    "imply it inspected observations that were not presented"
                ),
                "aggregate_fields_are_code_computed": True,
                "series_omissions_are_aggregate_represented": True,
                "interpretive_omissions_are_not_aggregated": True,
                "omission_counts_are_not_data_quality": True,
                "lineage_set_hash_is_an_identity_not_market_evidence": True,
                "scoring_lineage": "referenced by lineage.hash (not visible to models)",
            },
            "bounds": {
                "evidence_rows": 0,
                "evidence_omitted": 0,
                "view_bytes": 0,
                "truncations": truncations,
            },
        }
        budget = MAX_BODY_BYTES - len(canonical_json(skeleton).encode()) - 4096
        if budget <= 0:
            raise PackBuildError("PACK_TOO_LARGE")
        presented_ids: list[str] = []
        reasons: dict[str, int] = {}
        running = 0
        # Probe rows must not leave truncation receipts behind (review finding P3,
        # round 4): only the rows that are finally presented are recorded.
        probe_truncations: list[dict[str, Any]] = []

        def _admit(evidence_id: str) -> bool:
            nonlocal running
            if len(presented_ids) >= MAX_VIEW_EVIDENCE_ROWS:
                reasons["row_cap"] = reasons.get("row_cap", 0) + 1
                return False
            probe = self._evidence_row(inputs, evidence_id, {}, probe_truncations)
            if probe is None:
                reasons["structured_row_oversize"] = reasons.get("structured_row_oversize", 0) + 1
                return False
            size = len(canonical_json(probe).encode())
            if running + size > budget:
                reasons["byte_budget"] = reasons.get("byte_budget", 0) + 1
                return False
            presented_ids.append(evidence_id)
            running += size
            return True

        # Aggregate representatives are admitted FIRST. They are the only visible
        # form of the series, so the aggregate's "observations_presented" claim
        # must never be undone by interpretive rows consuming the cap afterwards
        # (which would leave advertised representatives uncitable).
        representatives_presented = 0
        for evidence_id in representative_ids:
            if evidence_id in presented_ids or evidence_id not in inputs.evidence_rows:
                continue
            if _admit(evidence_id):
                representatives_presented += 1
        for evidence_id in interpretive:
            _admit(evidence_id)
        # Supersession is view-local: it may only point at an observation that is
        # actually visible here (v1 semantics), so it is resolved after the
        # visible set is fixed.
        visible = set(presented_ids)
        successors = {
            row["supersedes_evidence_id"]: evidence_id
            for evidence_id, row in inputs.evidence_rows.items()
            if row["supersedes_evidence_id"] in visible
        }
        presented = [
            self._evidence_row(inputs, evidence_id, successors, truncations)
            for evidence_id in presented_ids
        ]
        presented = [row for row in presented if row is not None]
        visible_ids = {row["evidence_id"] for row in presented}
        # Trim the *shipped* copy of every aggregate and derive the per-screen
        # summary from that same object, so the two can never disagree and neither
        # can advertise an id that is not a citable evidence row (review findings
        # P1/P2, round 4). truncate_strings rebuilds the feature tree, so the trim
        # must resolve the aggregate inside the shipped copy, not the pre-copy
        # object the builder collected.
        for screen in screens:
            meta: list[dict[str, Any]] = []
            shipped = screen["raw_features"]
            for pair in screen_aggregates[screen["screen_result_id"]]:
                aggregate = resolve_aggregate(shipped, pair["path"])
                admitted = [
                    evidence_id
                    for evidence_id in aggregate["representative_evidence_ids"]
                    if evidence_id in visible_ids
                ]
                aggregate["representative_evidence_ids"] = admitted
                aggregate["representative_observations"] = [
                    observation
                    for observation in aggregate["representative_observations"]
                    if str(observation.get("evidence_id")) in visible_ids
                ]
                aggregate["observations_presented"] = len(admitted)
                aggregate["observations_omitted"] = aggregate["observation_count"] - len(admitted)
                series_not_frozen += aggregate["observations_not_frozen"]
                meta.append(
                    {
                        "path": pair["path"],
                        "observation_count": aggregate["observation_count"],
                        "observations_presented": aggregate["observations_presented"],
                        "observations_compacted": aggregate["observations_compacted"],
                        "observations_not_frozen": aggregate["observations_not_frozen"],
                        "observations_omitted": aggregate["observations_omitted"],
                        "session_date_range": aggregate["session_date_range"],
                        "lineage_set_hash": aggregate["lineage_set_hash"],
                        "representative_evidence_ids": admitted,
                    }
                )
            screen["series_aggregates"] = meta
            # Every screen, series or not, is reconciled with what was actually
            # admitted (review finding P2, round 4): a declared id that admission
            # declined must not be advertised as present with omitted == 0.
            screen["evidence_ids"] = [
                evidence_id for evidence_id in screen["evidence_ids"] if evidence_id in visible_ids
            ]
            screen["evidence_ids_omitted"] = screen["evidence_id_count"] - len(
                screen["evidence_ids"]
            )
        representative_presented = sum(
            1 for evidence_id in representative_ids if evidence_id in visible_ids
        )
        omitted_interpretive = sum(1 for item in interpretive if item not in visible_ids)
        omitted_series = max(0, len(aggregated_ids) - representative_presented)
        body = dict(skeleton)
        body["evidence"] = presented
        body["evidence_omitted"] = {
            "count": omitted_interpretive + omitted_series,
            "interpretive_omitted": omitted_interpretive,
            "series_observations_omitted": omitted_series,
            "series_references_not_frozen": series_not_frozen,
            "series_representatives_presented": representative_presented,
            "reasons": dict(sorted(reasons.items())),
            "semantics": skeleton["evidence_omitted"]["semantics"],
            "deterministic_order": (
                "interpretive_passing_first_then_evidence_id, then series representatives"
            ),
        }
        body["model_honesty"] = {
            **skeleton["model_honesty"],
            "evidence_presented": len(presented),
            "evidence_omitted": omitted_interpretive + omitted_series,
        }
        body["bounds"] = {
            "evidence_rows": len(presented),
            "evidence_omitted": omitted_interpretive + omitted_series,
            "view_bytes": 0,
            "truncations": truncations,
        }
        for _ in range(8):
            size = len(canonical_json(body))
            if body["bounds"]["view_bytes"] == size:
                break
            body["bounds"]["view_bytes"] = size
        encoded = canonical_json(body).encode()
        if len(encoded) > MAX_BODY_BYTES:
            raise PackBuildError("PACK_TOO_LARGE")
        return CommitteeView(hash_prefixed("committee-view-v1", body), body, lineage.lineage_hash)

    @staticmethod
    def _evidence_row(
        inputs: FrozenInputs,
        evidence_id: str,
        successors: dict[str, str],
        truncations: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        row = inputs.evidence_rows[evidence_id]
        try:
            fields = bound_structured(
                json.loads(row["structured_fields"]), evidence_id, truncations
            )
        except PackBuildError:
            # One unsalvageable interpretive payload must not block the whole
            # view: the observation stays complete in the scoring lineage and
            # the omission is recorded explicitly.
            truncations.append(
                {
                    "kind": "structured_row_omitted",
                    "evidence_id": evidence_id,
                    "bytes": len(str(row["structured_fields"]).encode()),
                }
            )
            return None
        return {
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
            "cluster_ids": list(inputs.clusters.get(evidence_id, [])),
            "underlying_group": inputs.groups.get(evidence_id),
            "freshness_days": _freshness_days(inputs.as_of, row["public_available_time"]),
        }

    def _persist(self, db: Any, candidate_id: str, view: CommitteeView) -> None:
        body_json = view.body_json
        expected = (
            view.pack_hash,
            VIEW_PACK_SPEC_VERSION,
            candidate_id,
            view.body["run"]["run_id"],
            body_json,
            len(body_json),
        )
        row = db.execute(
            "SELECT pack_hash,pack_spec_version,candidate_id,pipeline_run_id,body_json,body_chars "
            "FROM evidence_pack WHERE candidate_id=? AND pack_spec_version=?",
            (candidate_id, VIEW_PACK_SPEC_VERSION),
        ).fetchone()
        if row is not None:
            if tuple(row) != expected:
                raise DeterminismError("stored committee view differs from deterministic retry")
            return
        db.execute(
            "INSERT INTO evidence_pack(pack_hash,pack_spec_version,candidate_id,pipeline_run_id,"
            "body_json,body_chars,built_at) VALUES (?,?,?,?,?,?,?)",
            (*expected, utc_now()),
        )


__all__ = [
    "REPRESENTATION",
    "VIEW_PACK_SPEC_VERSION",
    "VIEW_SPEC_VERSION",
    "CommitteeView",
    "CommitteeViewBuilder",
    "aggregate_series",
]

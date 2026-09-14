"""Deterministic size bounds shared by every committee artifact (#67).

The bounds are split by artifact on purpose:

* structural/projection bounds (``MAX_STRUCTURED_KEYS``,
  ``MAX_STRUCTURED_BYTES``, ``MAX_STRING_CODEPOINTS``, ``MAX_FEATURE_SOURCES``)
  decide the *scoring identity projection* -- what the scorer and the
  methodology-change detector are allowed to see.  They are part of scoring
  semantics and are versioned (``SCORING_PROJECTION_VERSION``), never driven by
  a model-facing capacity limit.
* model-facing bounds (``MAX_BODY_BYTES`` = the bounded committee view body,
  ``MAX_VIEW_EVIDENCE_ROWS`` = the number of interpretive evidence rows the
  committee is shown) decide only what a model sees.  They must have **zero**
  effect on scoring inputs, on ``scored_evidence_hash`` or on trajectory
  identity.
"""

from __future__ import annotations

import hashlib
from typing import Any

from tradehub_research.screens import canonical_json

# --- scoring identity projection (part of scoring semantics) -----------------
SCORING_PROJECTION_VERSION = 1
MAX_FEATURE_SOURCES = 40
MAX_STRING_CODEPOINTS = 512
MAX_STRUCTURED_KEYS = 32
MAX_STRUCTURED_BYTES = 4096

# --- model-facing capacity bounds (view only) --------------------------------
MAX_BODY_BYTES = 160_000
MAX_VIEW_EVIDENCE_ROWS = 256

#: Scoring-identity row cap. **Independently declared on purpose** (review
#: finding P1, round 2): this is the cap that feeds
#: ``scoring.methodology_evidence_projection``, so it must never be an alias of
#: the model-facing ``MAX_VIEW_EVIDENCE_ROWS``. If it aliased the view bound, an
#: innocuous capacity change to what models see would silently move the
#: methodology identity of *historical* runs and manufacture
#: ``SCREEN_METHODOLOGY_CHANGE``. The two numbers happen to agree today; they are
#: allowed to diverge, and changing this one requires a deliberate
#: ``SEMANTIC_EVIDENCE_PROJECTION_VERSION`` decision.
MAX_EVIDENCE_ROWS = 256

#: Version of the methodology-identity projection applied to screen evidence ids
#: before hashing. See `scoring.methodology_evidence_projection`.
SEMANTIC_EVIDENCE_PROJECTION_VERSION = 1

#: Version of the confluence-group projection applied to scored evidence rows.
#: See `frozen_inputs.scoring_group_labels`.
SCORING_GROUP_PROJECTION_VERSION = 1

# Number of representative observations retained per aggregated series feature.
MAX_SERIES_REPRESENTATIVES = 4

TRUNCATION_SUFFIX = "…[truncated]"


def hash_prefixed(prefix: str, value: object) -> str:
    return hashlib.sha256((prefix + "\0" + canonical_json(value)).encode()).hexdigest()


def truncate_strings(value: Any, path: str, records: list[dict[str, Any]]) -> Any:
    if isinstance(value, str) and len(value) > MAX_STRING_CODEPOINTS:
        records.append({"kind": "string", "path": path, "original_codepoints": len(value)})
        return value[: MAX_STRING_CODEPOINTS - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
    if isinstance(value, list):
        return [
            truncate_strings(item, f"{path}/{index}", records) for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: truncate_strings(value[key], f"{path}/{key}", records) for key in sorted(value)
        }
    return value


def bound_structured(
    fields: dict[str, Any], evidence_id: str, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Bound one interpretive evidence payload. Raises on unsalvageable oversize."""
    from tradehub_research.committee.frozen_inputs import PackBuildError

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
    bounded = truncate_strings(
        {key: fields[key] for key in keys}, f"evidence/{evidence_id}/structured_fields", records
    )
    if len(canonical_json(bounded).encode()) > MAX_STRUCTURED_BYTES:
        raise PackBuildError("PACK_TOO_LARGE")
    return bounded


def bound_feature(value: Any, path: str, records: list[dict[str, Any]]) -> Any:
    """Bound a screen feature payload, keeping only the newest sources."""
    value = truncate_strings(value, path, records)
    if isinstance(value, dict):
        return {
            key: bound_feature(item, f"{path}/{key}", records)
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
        return [bound_feature(item, f"{path}/{index}", records) for index, item in enumerate(value)]
    return value


def count_series_observations(value: Any, path: str = "") -> int:
    """Count every ``/sources`` observation referenced by a feature payload."""
    total = 0
    if isinstance(value, dict):
        for key in sorted(value):
            total += count_series_observations(value[key], f"{path}/{key}")
    elif isinstance(value, list):
        if path.endswith("/sources"):
            return len(value)
        for index, item in enumerate(value):
            total += count_series_observations(item, f"{path}/{index}")
    return total


def series_observation_ids(value: Any, path: str = "") -> list[str]:
    """All observation (evidence) ids referenced by a feature payload, sorted."""
    ids: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            ids.extend(series_observation_ids(value[key], f"{path}/{key}"))
    elif isinstance(value, list):
        if path.endswith("/sources"):
            for item in value:
                if isinstance(item, dict) and item.get("evidence_id"):
                    ids.append(str(item["evidence_id"]))
            return ids
        for index, item in enumerate(value):
            ids.extend(series_observation_ids(item, f"{path}/{index}"))
    return sorted(set(ids))

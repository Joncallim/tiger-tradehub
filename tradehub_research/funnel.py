"""Deterministic candidate funnel (design section 5, algorithm ``funnel-v1``).

The funnel merges the mandatory set (holdings + passing event results),
a deterministic sector round-robin of signal securities, and a hash-sampled
control set into one candidate population.  It never drops a holding; a
mandatory overflow is retained and flagged ``budget_overflow_mandatory``.
No adaptive weights, no learned thresholds, no confluence bonus.

Cluster counting: rank telemetry resolves shared evidence clusters through a
caller-supplied ``cluster_lookup`` mapping evidence_id -> cluster_ids (built by
the orchestration layer with ONE bounded ``evidence_cluster_member`` join).
Cluster ids live only in rank telemetry, never in ``screen_result`` rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from tradehub_research.screens import SecurityId, canonical_json

FUNNEL_ALGORITHM = "funnel-v1"
CONTROL_ALGORITHM = "control-v1"
UNKNOWN_SECTOR = "UNKNOWN"

BASE_FAMILIES = ("valuation", "inflection", "quality", "informed_activity")
EVENT_FAMILY = "event"
MOMENTUM_FAMILY = "momentum_confirmation"


def _hash(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FunnelConfig:
    """Phase-1 funnel configuration; the canonical hash participates in run_id."""

    budget: int = 50
    control_count: int = 5
    algorithm: str = FUNNEL_ALGORITHM

    def as_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "budget": self.budget,
            "control_count": self.control_count,
        }

    @property
    def config_hash(self) -> str:
        return _hash(canonical_json(self.as_dict()))


@dataclass(frozen=True)
class FunnelResultRow:
    """One screen_result row for one security in one run, funnel-facing."""

    security_id: SecurityId
    family: str
    screen_result_id: str
    sufficient_data: bool
    passed: bool
    confidence: float
    data_quality: float
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    run_id: str
    security_id: SecurityId
    ordinal: int
    inclusion_reasons: list[str]
    screen_result_ids: list[str]
    rank_telemetry: dict[str, Any]
    is_control: bool
    control_algorithm: str | None
    control_key: str | None
    control_rank: int | None


def candidate_id(run_id: str, security_id: SecurityId) -> str:
    """Stable candidate identity, independent of presentation order."""
    return _hash(f"candidate-v1\0{run_id}\0{security_id}")


def control_key(logical_material: str, security_id: SecurityId) -> str:
    """sha256(control_seed || '\\0' || security_id), where the seed hashes the
    same logical material as run_id — input order cannot affect selection."""
    seed = _hash(f"{CONTROL_ALGORITHM}\0{logical_material}")
    return _hash(f"{seed}\0{security_id}")


def _sector_of(sectors: dict[SecurityId, str | None], security_id: SecurityId) -> str:
    sector = sectors.get(security_id)
    if sector is None or not str(sector).strip():
        return UNKNOWN_SECTOR
    return str(sector)


def run_funnel(
    *,
    run_id: str,
    logical_material: str,
    config: FunnelConfig,
    universe: list[SecurityId],
    holdings: set[SecurityId],
    results: list[FunnelResultRow],
    sectors: dict[SecurityId, str | None],
    cluster_lookup: dict[str, set[str]],
) -> tuple[list[Candidate], list[str]]:
    """Build the candidate population; returns (candidates, blocking_flags)."""
    by_security: dict[SecurityId, list[FunnelResultRow]] = {}
    for row in results:
        by_security.setdefault(row.security_id, []).append(row)

    # 1. Mandatory set: current holdings + passing event results, deduped.
    mandatory_reasons: dict[SecurityId, list[str]] = {}
    universe_set = set(universe)
    for security_id in sorted(holdings & universe_set):
        mandatory_reasons.setdefault(security_id, []).append("holding")
    for security_id, rows in by_security.items():
        if any(row.family == EVENT_FAMILY and row.passed for row in rows):
            mandatory_reasons.setdefault(security_id, []).append("event_pass")

    flags: list[str] = []
    # Holdings first, then event, by security_id (design section 5 step 5).
    holdings_sorted = sorted(s for s in mandatory_reasons if "holding" in mandatory_reasons[s])
    event_only_sorted = sorted(
        s for s in mandatory_reasons if "holding" not in mandatory_reasons[s]
    )
    mandatory = holdings_sorted + event_only_sorted
    if len(mandatory) > config.budget:
        # The only overflow: keep everything and block downstream launch.
        flags.append("budget_overflow_mandatory")
    mandatory_set = set(mandatory)

    # 2. Signal set: nonmandatory, passing >=1 base signal family.
    signal_security_set = {
        security_id
        for security_id, rows in by_security.items()
        if security_id not in mandatory_set
        and any(row.family in BASE_FAMILIES and row.passed for row in rows)
    }

    # 3. Controls: nonmandatory, non-signal, all five base results present,
    #    no base pass, at least one sufficient base result.
    control_eligible = []
    for security_id in universe:
        if security_id in mandatory_set or security_id in signal_security_set:
            continue
        rows = by_security.get(security_id, [])
        base = [row for row in rows if row.family in BASE_FAMILIES or row.family == EVENT_FAMILY]
        if {row.family for row in base} != set(BASE_FAMILIES) | {EVENT_FAMILY}:
            continue
        if any(row.passed for row in base):
            continue
        if not any(row.sufficient_data for row in base):
            continue
        control_eligible.append(security_id)
    control_eligible.sort(key=lambda sid: (control_key(logical_material, sid), sid))

    if flags:
        reserved_controls = 0
    else:
        reserved_controls = min(
            config.control_count, len(control_eligible), config.budget - len(mandatory)
        )
    controls = control_eligible[:reserved_controls]

    # 4. Signals ranked within sector; filled by deterministic round-robin.
    signal_capacity = max(config.budget - len(mandatory) - reserved_controls, 0)

    def clusters_for(security_id: SecurityId) -> set[str]:
        clusters: set[str] = set()
        for row in by_security.get(security_id, []):
            if not (row.family in BASE_FAMILIES and row.passed):
                continue
            for evidence_id in row.evidence_ids:
                clusters.update(cluster_lookup.get(evidence_id, set()))
        return clusters

    per_sector: dict[str, list[SecurityId]] = {}
    telemetry: dict[SecurityId, dict[str, Any]] = {}
    for security_id in sorted(signal_security_set):
        rows = by_security[security_id]
        base_rows = [row for row in rows if row.family in BASE_FAMILIES]
        passing = [row for row in base_rows if row.passed]
        clusters = clusters_for(security_id)
        momentum_passed = any(row.family == MOMENTUM_FAMILY and row.passed for row in rows)
        telemetry[security_id] = {
            "base_pass_count": sum(1 for row in base_rows if row.passed),
            "distinct_supporting_cluster_count": len(clusters),
            "shared_cluster_ids": sorted(clusters),
            "max_data_quality": max((row.data_quality for row in passing), default=0.0),
            "max_confidence": max((row.confidence for row in passing), default=0.0),
            "momentum_passed": momentum_passed,
        }
        per_sector.setdefault(_sector_of(sectors, security_id), []).append(security_id)

    for sector_rows in per_sector.values():
        sector_rows.sort(
            key=lambda sid: (
                -telemetry[sid]["base_pass_count"],
                -telemetry[sid]["distinct_supporting_cluster_count"],
                -telemetry[sid]["max_data_quality"],
                -telemetry[sid]["max_confidence"],
                not telemetry[sid]["momentum_passed"],
                sid,
            )
        )

    selected_signals: list[SecurityId] = []
    sectors_sorted = sorted(per_sector)
    cursors = {sector: 0 for sector in sectors_sorted}
    while len(selected_signals) < signal_capacity:
        progressed = False
        for sector in sectors_sorted:
            if len(selected_signals) >= signal_capacity:
                break
            cursor = cursors[sector]
            if cursor < len(per_sector[sector]):
                selected_signals.append(per_sector[sector][cursor])
                cursors[sector] = cursor + 1
                progressed = True
        if not progressed:
            break

    # 5. Merge reasons, assign ordinals: mandatory (holdings then event),
    #    signals in selection order, controls by control rank.
    ordered: list[tuple[SecurityId, list[str], bool]] = []
    for security_id in mandatory:
        passing_families = sorted(
            row.family for row in by_security.get(security_id, []) if row.passed
        )
        reasons = mandatory_reasons[security_id] + [f"{family}_pass" for family in passing_families]
        ordered.append((security_id, sorted(set(reasons)), False))
    for security_id in selected_signals:
        passing_families = sorted(
            row.family
            for row in by_security.get(security_id, [])
            if row.family in BASE_FAMILIES and row.passed
        )
        ordered.append(
            (security_id, ["signal", *[f"{family}_pass" for family in passing_families]], False)
        )
    for security_id in controls:
        ordered.append((security_id, ["control"], True))

    control_ranks = {sid: rank for rank, sid in enumerate(controls, start=1)}
    candidates: list[Candidate] = []
    for ordinal, (security_id, reasons, is_control) in enumerate(ordered, start=1):
        rows = by_security.get(security_id, [])
        result_ids = sorted(row.screen_result_id for row in rows)
        base_rows = [row for row in rows if row.family in BASE_FAMILIES]
        if is_control:
            rank_telemetry = {
                "base_pass_count": 0,
                "distinct_supporting_cluster_count": 0,
                "shared_cluster_ids": [],
                "max_data_quality": max((row.data_quality for row in base_rows), default=0.0),
                "max_confidence": max((row.confidence for row in base_rows), default=0.0),
                "momentum_passed": False,
            }
        else:
            rank_telemetry = telemetry.get(
                security_id,
                {
                    "base_pass_count": sum(1 for row in base_rows if row.passed),
                    "distinct_supporting_cluster_count": 0,
                    "shared_cluster_ids": [],
                    "max_data_quality": max((row.data_quality for row in base_rows), default=0.0),
                    "max_confidence": max((row.confidence for row in base_rows), default=0.0),
                    "momentum_passed": any(
                        row.family == MOMENTUM_FAMILY and row.passed for row in rows
                    ),
                },
            )
        candidates.append(
            Candidate(
                candidate_id=candidate_id(run_id, security_id),
                run_id=run_id,
                security_id=security_id,
                ordinal=ordinal,
                inclusion_reasons=reasons,
                screen_result_ids=result_ids,
                rank_telemetry=rank_telemetry,
                is_control=is_control,
                control_algorithm=CONTROL_ALGORITHM if is_control else None,
                control_key=control_key(logical_material, security_id) if is_control else None,
                control_rank=control_ranks.get(security_id) if is_control else None,
            )
        )
    return candidates, flags

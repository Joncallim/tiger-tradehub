"""Packet B: deterministic lookahead canaries.

Two classes of guard, both structural so a future refactor cannot silently
reintroduce leakage:

1. RUNTIME canaries: plant a known future-PAT evidence row (and a known
   adjusted-price value) into a fixture research.db and assert that the
   decision-time feature path (EvidenceStore.historical + the screening
   loaders used by run_screening) never surfaces it for a past as_of.

2. STATIC import-boundary canary: assert that the feature/decision modules
   (hunters/*, screens.py, screening.py) never import the outcome builder
   -- the future-outcome label path is structurally unreachable from the
   feature path, not merely unused by convention.

Results are written to experiment.db lookahead_canary_run (append-only).
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.evidence import EvidenceStore
from tradehub_research.validation.experiment_db import ExperimentDB

_FEATURE_MODULES = (
    "tradehub_research/hunters",
    "tradehub_research/screens.py",
    "tradehub_research/screening.py",
)
_FORBIDDEN_IMPORTS = (
    "tradehub_research.validation.outcome_builder",
    "tradehub_research.validation",
)


def run_runtime_canary(research_db: ResearchDB, experiment_db: ExperimentDB) -> dict[str, Any]:
    """Plant future-PAT evidence and assert the feature path never sees it.

    Returns {"canary_kind": "runtime_future_pat", "detected": 0|1, "detail"}.
    detected=1 means leakage WAS found (a failure).
    """
    store = EvidenceStore(research_db)
    with research_db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "canary", "derived_from_index"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "canary-sec",
                "CANARY",
                "NASDAQ",
                "Canary Inc.",
                "Technology",
                "Hardware",
                "SUPPORTED",
                "2024-01-01T00:00:00Z",
                None,
            ),
        )

    # Future evidence: PAT in 2030 (far beyond any plausible decision as_of).
    # The ingest time is also in the future -- this is a planted canary row,
    # not a real historical record, and the point is that a decision-time
    # as_of in the past must never see it.
    store.insert(
        security_id="canary-sec",
        source_id="tiingo_eod",
        structured_fields={"record_type": "price_bar", "session_date": "2030-01-15"},
        extraction_confidence=1.0,
        event_time="2030-01-15T20:15:00Z",
        public_available_time="2030-01-15T20:15:00Z",
        pat_provenance="derived_from_index",
        source_record_id="canary-future-bar",
        ingested_time="2030-01-15T21:00:00Z",
    )

    from tradehub_research.evidence import EvidenceStore as ES

    historical = ES(research_db).historical(as_of="2025-01-01T00:00:00Z")
    leaked = any(dict(row).get("source_record_id") == "canary-future-bar" for row in historical)

    detail = {"leaked_future_pat": leaked}
    return _record_canary(
        experiment_db,
        "runtime_future_pat",
        detected=1 if leaked else 0,
        detail=detail,
    )


def run_adjusted_price_canary(
    research_db: ResearchDB, experiment_db: ExperimentDB
) -> dict[str, Any]:
    """Assert the decision-time feature path never sees provider-adjusted
    outcome-side values (handoff sec 3.5: 'adjusted outcome values are never
    exposed back to feature generation')."""
    store = EvidenceStore(research_db)
    with research_db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "canary", "derived_from_index"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "canary-sec2",
                "CANARY2",
                "NASDAQ",
                "Canary Two Inc.",
                "Technology",
                "Hardware",
                "SUPPORTED",
                "2024-01-01T00:00:00Z",
                None,
            ),
        )
    store.insert(
        security_id="canary-sec2",
        source_id="tiingo_eod",
        structured_fields={
            "record_type": "price_bar",
            "session_date": "2024-01-15",
            "provider_adjusted_audit_only": {"adjClose": 999.0},
        },
        extraction_confidence=1.0,
        event_time="2024-01-15T20:15:00Z",
        public_available_time="2024-01-15T20:15:00Z",
        pat_provenance="derived_from_index",
        source_record_id="canary-adjusted-bar",
        ingested_time="2024-01-15T21:00:00Z",
    )

    from tradehub_research.screening import _load_record_kind

    with research_db.connect(read_only=True) as db:
        loaded = _load_record_kind(db, "2025-01-01T00:00:00Z", ["canary-sec2"], "price_bar")
    adjusted_leaked = any(
        "provider_adjusted_audit_only" in fields for fields in loaded.get("canary-sec2", [])
    )

    return _record_canary(
        experiment_db,
        "adjusted_price_leak",
        detected=1 if adjusted_leaked else 0,
        detail={"adjusted_leaked_into_features": adjusted_leaked},
    )


def run_static_import_boundary_canary(
    repo_root: Path, experiment_db: ExperimentDB
) -> dict[str, Any]:
    """Static check: feature modules must never import the outcome builder."""
    violations: list[str] = []
    for relative in _FEATURE_MODULES:
        path = repo_root / relative
        if not path.exists():
            violations.append(f"{relative}: missing")
            continue
        files = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for source_file in files:
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if any(node.module.startswith(f) for f in _FORBIDDEN_IMPORTS):
                        violations.append(
                            f"{source_file.relative_to(repo_root)} imports {node.module}"
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name.startswith(f) for f in _FORBIDDEN_IMPORTS):
                            violations.append(
                                f"{source_file.relative_to(repo_root)} imports {alias.name}"
                            )
    return _record_canary(
        experiment_db,
        "static_import_boundary",
        detected=1 if violations else 0,
        detail={"violations": violations},
    )


def _record_canary(
    experiment_db: ExperimentDB,
    kind: str,
    *,
    detected: int,
    detail: dict[str, Any],
    dataset_snapshot_id: str = "canary",
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    row = {
        "run_id": run_id,
        "dataset_snapshot_id": dataset_snapshot_id,
        "canary_kind": kind,
        "detected": detected,
        "detail_json": json.dumps(detail, sort_keys=True),
        "run_at": utc_now(),
    }
    with experiment_db.connect() as conn:
        # Canaries are structural guards; if no dataset_snapshot row exists
        # yet (common when running pre-snapshot), register a placeholder
        # snapshot so the FK contract holds without inventing semantics.
        existing = conn.execute(
            "SELECT 1 FROM dataset_snapshot WHERE snapshot_id=?", (dataset_snapshot_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO dataset_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dataset_snapshot_id,
                    "canary",
                    0,
                    None,
                    "{}",
                    "placeholder",
                    "",
                    "",
                    "{}",
                    "READY",
                    utc_now(),
                ),
            )
        conn.execute(
            "INSERT INTO lookahead_canary_run VALUES (?,?,?,?,?,?)",
            (
                row["run_id"],
                row["dataset_snapshot_id"],
                row["canary_kind"],
                row["detected"],
                row["detail_json"],
                row["run_at"],
            ),
        )
    row["detail"] = detail
    return row

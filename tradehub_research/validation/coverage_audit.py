"""Programmatic data-sufficiency audit -- makes docs/phase5-coverage-audit.md
re-derivable and testable instead of a static artifact that can drift from
reality.

Read-only: connects to research.db in read-only mode, checks Tiingo/SEC
credential presence, and the Tiingo adapter's own durable bootstrap-quota
state. Emits a structured report classifying each Hunter family's evidence
posture per handoff docs/phase5-validation-research-architecture-handoff.md
section 4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB

# Row-count tables that indicate SOME ingested evidence exists.
_CORE_TABLES = (
    "security",
    "security_identity_event",
    "evidence_event",
    "evidence_source",
    "universe_membership",
    "screen_result",
    "screen_definition",
    "candidate",
    "pipeline_run",
    "score_snapshot",
    "model_assessment",
    "trade_proposal",
    "portfolio_state_observation",
)

# Handoff section 4: which evidence_event record_type(s) each Hunter family needs.
_HUNTER_FAMILY_EVIDENCE_KINDS: dict[str, tuple[str, ...]] = {
    "momentum": ("price_bar",),
    "valuation": ("price_bar", "xbrl_fact"),
    "quality": ("xbrl_fact",),
    "inflection": ("xbrl_fact",),
    "informed_activity": ("form4_transaction",),
    "event": ("xbrl_fact", "form4_transaction"),
}


def _table_row_counts(db: ResearchDB) -> dict[str, int]:
    counts: dict[str, int] = {}
    with db.connect(read_only=True) as conn:
        existing = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in _CORE_TABLES:
            if table not in existing:
                counts[table] = -1  # table doesn't exist in this schema version
                continue
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    return counts


def _evidence_kind_counts(db: ResearchDB) -> dict[str, int]:
    """Row counts of evidence_event grouped by structured_fields.record_type."""
    counts: dict[str, int] = {}
    with db.connect(read_only=True) as conn:
        existing = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "evidence_event" not in existing:
            return counts
        rows = conn.execute(
            "SELECT json_extract(structured_fields,'$.record_type') AS kind, COUNT(*) "
            "FROM evidence_event WHERE withdrawn=0 GROUP BY kind"
        ).fetchall()
    for kind, count in rows:
        if kind is not None:
            counts[str(kind)] = int(count)
    return counts


def _classify_hunter_family(kind_counts: dict[str, int], kinds: tuple[str, ...]) -> str:
    """EVALUABLE if every required evidence kind has rows; ZERO_EVALUABLE if none
    do; PARTIAL if some but not all required kinds have rows."""
    present = [kind_counts.get(kind, 0) > 0 for kind in kinds]
    if all(present):
        return "EVALUABLE"
    if any(present):
        return "PARTIAL"
    return "ZERO_EVALUABLE"


def _tiingo_bootstrap_state(cache_dir: Path) -> dict[str, Any]:
    from tradehub_research.adapters.tiingo import TiingoQuota

    state_path = cache_dir / "tiingo-operational.sqlite"
    if not state_path.exists():
        return {"state_file_exists": False, "used": 0, "remaining": 450, "limit": 450}
    import time

    quota = TiingoQuota(state_path=state_path)
    usage = quota.bootstrap_usage(time.time())
    return {"state_file_exists": True, **usage}


def run_coverage_audit(
    settings: ResearchSettings | None = None, database: ResearchDB | None = None
) -> dict[str, Any]:
    """Run the live data-sufficiency audit and return a structured report.

    This is the programmatic equivalent of docs/phase5-coverage-audit.md --
    call it to re-derive the audit at any point instead of trusting a static
    document that can drift from reality.
    """
    settings = settings or ResearchSettings()
    database = database or ResearchDB(settings.db_path, settings.busy_timeout_ms)

    table_counts = _table_row_counts(database)
    evidence_kind_counts = _evidence_kind_counts(database)
    hunter_posture = {
        family: _classify_hunter_family(evidence_kind_counts, kinds)
        for family, kinds in _HUNTER_FAMILY_EVIDENCE_KINDS.items()
    }

    tiingo_configured = bool(settings.tiingo_token) and settings.tiingo_license_confirmed
    sec_configured = bool(settings.sec_user_agent)
    tiingo_bootstrap = _tiingo_bootstrap_state(settings.adapter_cache_dir)

    any_evidence = table_counts.get("evidence_event", -1) > 0
    any_screens = table_counts.get("screen_result", -1) > 0

    return {
        "database_path": str(database.path),
        "schema_version": database.schema_version(),
        "table_row_counts": table_counts,
        "evidence_kind_counts": evidence_kind_counts,
        "hunter_family_posture": hunter_posture,
        "tiingo_credentials_configured": tiingo_configured,
        "sec_credentials_configured": sec_configured,
        "tiingo_bootstrap_usage": tiingo_bootstrap,
        "any_evidence_ingested": any_evidence,
        "any_screens_run": any_screens,
        "overall_posture": "ZERO_EVALUABLE" if not any_evidence else "PARTIAL_OR_BETTER",
    }

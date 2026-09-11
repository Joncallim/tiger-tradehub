"""Operator / read-only status surface (issue #39 B1).

One compact deterministic summary covering the seven operator concepts:
research_status, pipeline_status, candidates/current_changes, portfolio_status,
proposal_status, validation/forward-learning status, report/status.

No credentials, no execution, no raw filings -- sanitized summaries only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.health import forward_health, refresh_health
from tradehub_research.validation.experiment_db import ExperimentDB

RUNNER_RECEIPTS = Path("/var/lib/tradehub-research/autonomy/paper_run_ledger.jsonl")


def _runner_receipts() -> dict[str, Any]:
    """Sanitized receipt count/latest entry; malformed lines never imply success."""
    if not RUNNER_RECEIPTS.exists():
        return {"count": 0, "latest": None, "status": "unavailable"}
    latest = None
    count = 0
    for line in RUNNER_RECEIPTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return {"count": count, "latest": latest, "status": "malformed"}
        count += 1
        latest = {
            key: item.get(key)
            for key in (
                "proposal_id",
                "decision",
                "dry_run",
                "submitted",
                "order_id",
                "reconcile_status",
                "at",
            )
        }
    return {"count": count, "latest": latest, "status": "ok"}


def operator_status(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
) -> dict:
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    fwd = forward_health(experiment_db=experiment_db, paths=paths)
    refr = refresh_health(settings=settings, paths=paths)

    # Pipeline: last research-cycle run from the cycle log.
    cycle_log = paths.research_dir / "cycle-log.jsonl"
    last_cycle = None
    if cycle_log.exists():
        lines = [ln for ln in cycle_log.read_text().splitlines() if ln.strip()]
        if lines:
            try:
                last_cycle = json.loads(lines[-1])
            except ValueError:
                last_cycle = None

    # Decision ledger: a no-proposal run is first-class, not an error.
    proposal = None
    chain = {"portfolio_runs": 0, "proposals": 0, "eligible_exports": 0}
    with research_db.connect(read_only=True) as conn:
        try:
            row = conn.execute(
                "SELECT run_id, created_at FROM portfolio_run ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if row:
                run_id = str(row["run_id"])
                proposal = {"run_id": run_id, "created_at": str(row["created_at"])}
                chain["portfolio_runs"] = 1
                chain["proposals"] = conn.execute(
                    "SELECT count(*) FROM trade_proposal WHERE decision_id IN "
                    "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
                    (run_id,),
                ).fetchone()[0]
                chain["eligible_exports"] = sum(
                    1
                    for item in (last_cycle or {}).get("decision", {}).get("eligible_exports", [])
                    if isinstance(item, str)
                )
        except Exception:  # noqa: BLE001 -- optional table
            proposal = None

    # Validation: regime + snapshot presence.
    validation = {}
    with experiment_db.connect(read_only=True) as conn:
        try:
            reg = conn.execute(
                "SELECT regime_id, status, sealed_at FROM evaluation_regime "
                "ORDER BY sealed_at DESC LIMIT 1"
            ).fetchone()
            validation["regime"] = {
                "regime_id": str(reg["regime_id"]) if reg else None,
                "status": str(reg["status"]) if reg else None,
                "sealed_at": str(reg["sealed_at"]) if reg else None,
            }
        except Exception:  # noqa: BLE001
            validation["regime"] = None
        try:
            snap = conn.execute(
                "SELECT snapshot_id, source_commit, created_at FROM dataset_snapshot "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            validation["snapshot"] = {
                "snapshot_id": str(snap["snapshot_id"]) if snap else None,
                "source_commit": str(snap["source_commit"]) if snap else None,
            }
        except Exception:  # noqa: BLE001
            validation["snapshot"] = None

    return {
        "generated_at": utc_now(),
        "research_status": {
            "universe_eligible": refr["securities_expected"],
            "with_price_history": refr["with_bars"],
            "stale_names": refr["stale_count"],
        },
        "pipeline_status": (
            {
                "last_cycle_as_of": last_cycle.get("as_of"),
                "last_cycle_status": last_cycle.get("status"),
                "candidates": last_cycle.get("candidate_count"),
                "screens": last_cycle.get("screens"),
            }
            if last_cycle
            else {"last_cycle": None}
        ),
        "candidates_current": (last_cycle.get("candidates", []) if last_cycle else []),
        "portfolio_status": {
            "last_run": proposal,
            "decision_status": (last_cycle or {}).get("decision", {}).get("status"),
        },
        "proposal_status": {
            "eligible_exports": chain["eligible_exports"],
            "classification": (last_cycle or {}).get("decision", {}).get("status"),
        },
        "decision_chain": {
            "research_cycle": (last_cycle or {}).get("status"),
            **chain,
            "runner_receipts": _runner_receipts(),
        },
        "validation_forward": {
            "production_predictions": fwd["production_predictions"],
            "predictions_due": fwd["predictions_due"],
            "matured": sum(fwd.get("matured", {}).values()),
            **validation,
        },
        "report_status": {
            "daily": "enabled (Hermes cron 23:00)",
            "weekly": "enabled (Fri 23:00)",
            "broker_analytics": (
                "available"
                if Path("/var/lib/tradehub/analytics/latest.json").exists()
                else "unavailable"
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    exp = ExperimentDB(research_paths().experiment_db)
    print(json.dumps(operator_status(settings=settings, experiment_db=exp), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

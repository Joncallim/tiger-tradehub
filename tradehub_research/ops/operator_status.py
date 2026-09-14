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
from tradehub_research.ops.acceptance_rows import (
    ACCEPTANCE_RUN_PREFIXES,
    genuine_clause,
)
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.decision_pipeline import DEFAULT_AUTHORITY_DIR
from tradehub_research.ops.health import forward_health, refresh_health
from tradehub_research.validation.experiment_db import ExperimentDB

RUNNER_RECEIPTS = Path("/var/lib/tradehub-research/autonomy/paper_run_ledger.jsonl")


def _authority_record_count() -> int:
    """Durable count of published authority records (export proof)."""
    if not DEFAULT_AUTHORITY_DIR.is_dir():
        return 0
    try:
        return sum(1 for path in DEFAULT_AUTHORITY_DIR.glob("*.json") if path.is_file())
    except OSError:
        return 0


def _all_total(entry: dict | None) -> int | None:
    """Lifetime total for a provenance entry, or None when the split is UNKNOWN.

    Never coerces an unknown split to a fake 0, and never raises: a failed
    provenance query must leave the operator status surface fully intact.
    """
    if not isinstance(entry, dict):
        return None
    genuine, acceptance = entry.get("genuine"), entry.get("acceptance")
    if genuine is None or acceptance is None:
        return None
    return int(genuine) + int(acceptance)


def _published_authority_ids() -> set[str]:
    if not DEFAULT_AUTHORITY_DIR.is_dir():
        return set()
    try:
        return {p.stem for p in DEFAULT_AUTHORITY_DIR.glob("*.json") if p.is_file()}
    except OSError:
        return set()


def _authority_record_count() -> int:
    """LIFETIME global count of published authority records."""
    return len(_published_authority_ids())


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

    # Decision ledger: a no-proposal run is first-class, not an error. EVERY
    # field is derived from durable state, so asynchronous finalizer work can
    # never make the chain inconsistent with a stale cycle-log snapshot.
    # LIFETIME/GLOBAL totals carry an explicit `_total` suffix; per-decision
    # values live under `latest_decision`.
    proposal = None
    latest_run_id: str | None = None
    latest_pipeline_run_id: str | None = None
    latest_decision_as_of: str | None = None
    latest_created_at: str | None = None
    latest_proposals = 0
    # ACCEPTANCE rows are durable, append-only deployment-verification rows.
    # They are reported separately and NEVER mixed into genuine-production
    # counts, portfolio performance, forward-learning results, evidence
    # conclusions, or adaptive training/evaluation inputs.
    gsql, gparams = genuine_clause()
    obs_sql, obs_params = genuine_clause(column="r.pipeline_run_id")
    committee_sql, committee_params = genuine_clause(column="c.pipeline_run_id")
    provenance: dict[str, Any] = {
        "acceptance_run_prefixes": list(ACCEPTANCE_RUN_PREFIXES),
        "portfolio_runs": {"genuine": 0, "acceptance": 0},
        "observations": {"genuine": 0, "acceptance": 0},
        "score_snapshots": {"genuine": 0, "acceptance": 0},
        "committee_runs": {"genuine": 0, "acceptance": 0},
    }
    chain: dict[str, Any] = {
        "portfolio_runs_total": 0,
        "observations_total": 0,
        "authority_records_total": 0,
        "latest_decision": None,
    }
    with research_db.connect(read_only=True) as conn:
        try:
            chain["portfolio_runs_total"] = conn.execute(
                "SELECT count(*) FROM portfolio_run WHERE " + gsql, gparams
            ).fetchone()[0]
            chain["observations_total"] = conn.execute(
                "SELECT count(*) FROM portfolio_state_observation o JOIN portfolio_run r "
                "ON r.run_id = o.run_id WHERE " + obs_sql,
                obs_params,
            ).fetchone()[0]
            row = conn.execute(
                "SELECT run_id, pipeline_run_id, decision_as_of, created_at "
                "FROM portfolio_run WHERE " + gsql + " "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                gparams,
            ).fetchone()
            if row:
                latest_run_id = str(row["run_id"])
                latest_pipeline_run_id = str(row["pipeline_run_id"])
                latest_decision_as_of = str(row["decision_as_of"])
                latest_created_at = str(row["created_at"])
                proposal = {
                    "run_id": latest_run_id,
                    "pipeline_run_id": latest_pipeline_run_id,
                    "decision_as_of": latest_decision_as_of,
                    "created_at": latest_created_at,
                }
                latest_proposals = 0
        except Exception:  # noqa: BLE001 -- optional table
            proposal = None
    chain["authority_records_total"] = _authority_record_count()
    # Genuine vs acceptance split for the decision chain. Every table is filtered
    # through an EXPLICITLY QUALIFIED column on the table that actually owns the
    # pipeline run id:
    #   portfolio_run                 -> pipeline_run_id (own column)
    #   portfolio_state_observation   -> via portfolio_run.run_id
    #   score_snapshot                -> via committee_run (has NEITHER
    #                                    pipeline_run_id NOR run_id)
    #   committee_run                 -> pipeline_run_id (own column)
    # A wrong column here raises OperationalError, so failures are RECORDED
    # rather than swallowed: a silent zero would be a misleading report.
    provenance_errors: dict[str, str] = {}
    provenance_specs = (
        ("portfolio_runs", "portfolio_run", "SELECT count(*) FROM portfolio_run", gsql, gparams),
        (
            "observations",
            "portfolio_state_observation o JOIN portfolio_run r ON r.run_id = o.run_id",
            "SELECT count(*) FROM portfolio_state_observation",
            obs_sql,
            obs_params,
        ),
        (
            "score_snapshots",
            "score_snapshot s JOIN committee_run c ON c.committee_run_id = s.committee_run_id",
            "SELECT count(*) FROM score_snapshot",
            committee_sql,
            committee_params,
        ),
        ("committee_runs", "committee_run", "SELECT count(*) FROM committee_run", gsql, gparams),
    )
    for key, source, total_sql, where_sql, where_params in provenance_specs:
        try:
            with research_db.connect(read_only=True) as conn:
                total = conn.execute(total_sql).fetchone()[0]
                genuine = conn.execute(
                    f"SELECT count(*) FROM {source} WHERE {where_sql}", where_params
                ).fetchone()[0]
                provenance[key] = {
                    "genuine": int(genuine),
                    "acceptance": int(total) - int(genuine),
                }
        except Exception as exc:  # noqa: BLE001 -- reported, never silent
            provenance[key] = {"genuine": None, "acceptance": None}
            provenance_errors[key] = f"{type(exc).__name__}: {exc}"
    provenance["query_errors"] = provenance_errors
    chain["provenance"] = provenance
    # ``*_total`` fields below are GENUINE-production totals (acceptance rows
    # excluded). The untouched raw grand totals are exposed separately under
    # ``*_all_total`` so no consumer silently loses the ability to see them.
    # Both are None when the split is UNKNOWN — never a fake 0, and never a
    # crash: a failed provenance query must not take the whole status down.
    chain["portfolio_runs_all_total"] = _all_total(provenance["portfolio_runs"])
    chain["observations_all_total"] = _all_total(provenance["observations"])
    # The LATEST DECISION's eligible exports are derived from durable state:
    # the proposals persisted for that run, intersected with the published
    # authority records. Deliberately NOT the lifetime authority-file count and
    # deliberately NOT the cycle log, so an asynchronous finalizer run is
    # reflected immediately even when cycle-log.jsonl has not changed.
    exported_ids: list[str] = []
    if latest_run_id is not None:
        with research_db.connect(read_only=True) as conn:
            rows = conn.execute(
                "SELECT proposal_id FROM trade_proposal WHERE decision_id IN "
                "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
                (latest_run_id,),
            ).fetchall()
        proposal_ids = {str(r["proposal_id"]) for r in rows}
        latest_proposals = len(proposal_ids)
        exported_ids = sorted(_published_authority_ids() & proposal_ids)
    chain["latest_decision"] = {
        "run_id": latest_run_id,
        "pipeline_run_id": latest_pipeline_run_id,
        "decision_as_of": latest_decision_as_of,
        "created_at": latest_created_at,
        "proposals": latest_proposals,
        "eligible_exports": len(exported_ids),
        "exported_proposal_ids": exported_ids,
    }

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
            # Latest decision's durable export count (NOT the lifetime total).
            "eligible_exports": (chain["latest_decision"] or {}).get("eligible_exports", 0),
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

"""Operator / read-only status surface (issue #39 B1).

One compact deterministic summary covering the seven operator concepts:
research_status, pipeline_status, candidates/current_changes, portfolio_status,
proposal_status, validation/forward-learning status, report/status.

No credentials, no execution, no raw filings -- sanitized summaries only.
"""

from __future__ import annotations

import json
import stat
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
    try:
        return int(genuine) + int(acceptance)
    except (TypeError, ValueError):
        return None


def _published_authority_ids() -> set[str]:
    """Ids (filename stems) of the published proposal-authority records.

    The UNKNOWN state is deliberately NOT representable as an empty set here:

    * authority directory ABSENT   -> ``set()``: nothing has ever been
      published (a documented, KNOWN-empty absence);
    * directory present and empty  -> ``set()``: known zero;
    * ANY read failure (EACCES on the directory OR on a parent path component,
      an unlistable directory, an unstattable entry, an I/O error) -> the
      ``OSError`` PROPAGATES to the guarded callers, which record an explicit
      UNKNOWN (``None``) plus the error. An unreadable authority store must
      never be reported as "zero authority records" or "this decision exported
      nothing".

    Two CPython behaviours make the "obvious" implementation silently wrong, and
    both were caught by the hosted 3.10/3.11/3.12 gate (invisible on the 3.14
    development venv):

    * ``Path.is_dir()``/``Path.exists()`` SWALLOW EACCES and return ``False``,
      so an unreadable store reads as an absent one -- hence the explicit
      ``stat()`` below, with only ``FileNotFoundError`` treated as absence;
    * ``Path.glob()`` SWALLOWS the EACCES raised while iterating an unreadable
      directory and yields nothing -- hence the explicit ``iterdir()``.
    """
    directory = DEFAULT_AUTHORITY_DIR
    try:
        info = directory.stat()
    except FileNotFoundError:
        return set()
    if not stat.S_ISDIR(info.st_mode):
        # NOT the documented absence: the authority store exists but is not a
        # directory, which is a configuration error worth surfacing (a stale or
        # wrong path must not read as "nothing was ever published").
        raise NotADirectoryError(20, "authority store is not a directory", str(directory))
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        # Raced away after stat(): genuinely nothing published.
        return set()
    ids: set[str] = set()
    for entry in entries:
        name = entry.name
        if name.startswith(".") or not name.endswith(".json"):
            continue
        # stat() through the entry, so an UNSTATTABLE record is an error rather
        # than a silently shrunken store.
        if stat.S_ISREG(entry.stat().st_mode):
            ids.add(entry.stem)
    return ids


def _authority_record_count() -> int:
    """LIFETIME global count of published authority records.

    Propagates any read failure so the caller can record UNKNOWN rather than a
    fake zero.
    """
    return len(_published_authority_ids())


def _field(payload: object, key: str) -> Any:
    """A health-payload field, or None -- a partial payload never crashes."""
    return payload.get(key) if isinstance(payload, dict) else None


def _matured_total(payload: object) -> int | None:
    """Total matured outcomes, or None when the health payload is unusable."""
    matured = payload.get("matured") if isinstance(payload, dict) else None
    if not isinstance(matured, dict):
        return None
    try:
        return sum(int(v) for v in matured.values())
    except (TypeError, ValueError):
        return None


def _cycle_decision_status(entry: dict | None, errors: dict[str, str] | None = None) -> str | None:
    """The HISTORICAL cycle-log decision status, or None.

    Deliberately never presented as the current durable decision status: the
    async finalizer can advance the durable decision ledger without rewriting
    ``cycle-log.jsonl``, so this value can be arbitrarily stale.

    A structurally corrupt ``decision`` value is RECORDED (when an ``errors``
    sink is supplied) rather than silently reported as "no status".
    """
    decision = (entry or {}).get("decision")
    if decision is None or not isinstance(decision, dict):
        if decision is not None and errors is not None:
            errors["cycle_log_decision"] = (
                f"cycle-log decision is a JSON {type(decision).__name__}, not an object"
            )
        return None
    status = decision.get("status")
    return None if status is None else str(status)


def _cycle_log_snapshot(path: Path, errors: dict[str, str]) -> dict | None:
    """Last research-cycle log entry, or None with the reason recorded.

    Three outcomes stay DISTINGUISHABLE, because "no cycle has ever run" must
    never be how a broken log renders:

    * log ABSENT          -> None, no error (a legitimate absence);
    * unreadable / bad JSON -> None + ``errors["cycle_log"]``;
    * valid JSON that is not an object -> None + ``errors["cycle_log"]``
      (a corrupt entry must not crash the whole surface on ``.get``).
    """
    try:
        path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        errors["cycle_log"] = f"{type(exc).__name__}: {exc}"
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors["cycle_log"] = f"{type(exc).__name__}: {exc}"
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        entry = json.loads(lines[-1])
    except ValueError as exc:
        errors["cycle_log"] = f"malformed last line: {exc}"
        return None
    if not isinstance(entry, dict):
        errors["cycle_log"] = f"last line is a JSON {type(entry).__name__}, not an object"
        return None
    return entry


def _path_state(path: Path) -> str:
    """``available`` / ``absent`` / ``not_a_regular_file`` / ``stat_error:<Exc>``.

    ``Path.is_file()`` collapses "not there" and "not a regular file" into one
    False, so the state is derived from an explicit ``stat`` instead: the failure
    and the unexpected-shape cases stay distinguishable from a documented absence.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"stat_error:{type(exc).__name__}"
    return "available" if stat.S_ISREG(info.st_mode) else "not_a_regular_file"


def _runner_receipts() -> dict[str, Any]:
    """Sanitized receipt summary; a broken ledger never implies success.

    Outcomes stay DISTINGUISHABLE and never raise out of operator_status():

    * ledger ABSENT    -> status ``unavailable``, count 0 (documented absence: no
      run has ever recorded a receipt);
    * ledger UNREADABLE -> status ``unreadable:<Exc>``, count ``None``;
    * a line that is not JSON, or is JSON but NOT an object -> status
      ``malformed``, count ``None`` -- the number of good lines is not a
      trustworthy total, so it is reported as UNKNOWN rather than as a partial
      count that reads like a real one.
    """
    try:
        if not RUNNER_RECEIPTS.exists():
            return {"count": 0, "latest": None, "status": "unavailable"}
        text = RUNNER_RECEIPTS.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {"count": None, "latest": None, "status": f"unreadable:{type(exc).__name__}"}
    latest = None
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return {"count": None, "latest": latest, "status": "malformed"}
        if not isinstance(item, dict):
            # e.g. a stray list/number/string line: `.get` would raise and take
            # the whole operator surface down.
            return {"count": None, "latest": latest, "status": "malformed"}
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
    # Any failure anywhere below must be RECORDED here rather than escaping
    # operator_status() or masquerading as "the system is empty". Declared early
    # so every section can contribute; chain["query_errors"] aliases this object.
    chain_errors: dict[str, str] = {}

    try:
        fwd = forward_health(experiment_db=experiment_db, paths=paths)
    except Exception as exc:  # noqa: BLE001 -- recorded, never silent
        chain_errors["forward_health"] = f"{type(exc).__name__}: {exc}"
        # `matured` is None, NOT {} -- an empty mapping would render as the
        # KNOWN value 0 matured outcomes, which is a different (false) claim.
        fwd = {"production_predictions": None, "predictions_due": None, "matured": None}
    try:
        refr = refresh_health(settings=settings, paths=paths)
    except Exception as exc:  # noqa: BLE001 -- recorded, never silent
        chain_errors["refresh_health"] = f"{type(exc).__name__}: {exc}"
        refr = {"securities_expected": None, "with_bars": None, "stale_count": None}

    # Pipeline: last research-cycle run from the cycle log. A CORRUPT or
    # UNREADABLE log must be distinguishable from "no cycle has ever run";
    # the helper records the reason and never lets a broken log crash the
    # whole surface.
    cycle_log = paths.research_dir / "cycle-log.jsonl"
    last_cycle = _cycle_log_snapshot(cycle_log, chain_errors)

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

    # A failed DECISION-LEDGER query must be OBSERVABLE, never a silent 0 that is
    # indistinguishable from a genuinely empty system. Each field is guarded
    # independently and falls back to None with a recorded error.
    def _ledger_scalar(label: str, sql: str, params: tuple = ()):
        try:
            with research_db.connect(read_only=True) as conn:
                return conn.execute(sql, params).fetchone()[0]
        except Exception as exc:  # noqa: BLE001 -- recorded, never silent
            chain_errors[label] = f"{type(exc).__name__}: {exc}"
            return None

    chain["portfolio_runs_total"] = _ledger_scalar(
        "portfolio_runs_total", "SELECT count(*) FROM portfolio_run WHERE " + gsql, gparams
    )
    chain["observations_total"] = _ledger_scalar(
        "observations_total",
        "SELECT count(*) FROM portfolio_state_observation o JOIN portfolio_run r "
        "ON r.run_id = o.run_id WHERE " + obs_sql,
        obs_params,
    )
    latest_decision_ok = True
    try:
        with research_db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT run_id, pipeline_run_id, decision_as_of, created_at "
                "FROM portfolio_run WHERE " + gsql + " "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                gparams,
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 -- recorded, never silent
        chain_errors["latest_decision"] = f"{type(exc).__name__}: {exc}"
        row = None
        latest_decision_ok = False
    chain["query_errors"] = chain_errors
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
    # An UNREADABLE authority store is UNKNOWN, never a fake zero: the helper
    # propagates the OSError and it is recorded here.
    try:
        chain["authority_records_total"] = _authority_record_count()
    except Exception as exc:  # noqa: BLE001 -- recorded, never silent
        chain["authority_records_total"] = None
        chain_errors["authority_records_total"] = f"{type(exc).__name__}: {exc}"
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
    #
    # The two failure modes are guarded SEPARATELY so each produces its own
    # honest UNKNOWN: a broken proposal query must not be blamed on the
    # authority store, and an UNREADABLE authority store must not be reported
    # as "this decision exported nothing" (its ids are unknown, while the
    # proposal count stays a known fact).
    exported_ids: list[str] | None = []
    proposal_ids: set[str] | None = None
    if latest_run_id is not None:
        try:
            with research_db.connect(read_only=True) as conn:
                rows = conn.execute(
                    "SELECT proposal_id FROM trade_proposal WHERE decision_id IN "
                    "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
                    (latest_run_id,),
                ).fetchall()
            proposal_ids = {str(r["proposal_id"]) for r in rows}
            latest_proposals = len(proposal_ids)
        except Exception as exc:  # noqa: BLE001 -- recorded, never silent
            # A failed export lookup must NOT take the whole status down, and
            # must not masquerade as "this decision exported nothing".
            chain_errors["eligible_exports"] = f"{type(exc).__name__}: {exc}"
            latest_proposals = None
            exported_ids = None
        if proposal_ids is not None:
            try:
                published = _published_authority_ids()
            except Exception as exc:  # noqa: BLE001 -- recorded, never silent
                chain_errors["eligible_exports"] = f"authority_store:{type(exc).__name__}: {exc}"
                published = None
            if published is None:
                exported_ids = None
            else:
                exported_ids = sorted(published & proposal_ids)
    elif not latest_decision_ok:
        # The row fetch itself failed, so "no decision" would be a LIE. Report
        # the dependent fields as unknown rather than as a legitimate zero.
        latest_proposals = None
        exported_ids = None
    chain["latest_decision"] = {
        "run_id": latest_run_id,
        "pipeline_run_id": latest_pipeline_run_id,
        "decision_as_of": latest_decision_as_of,
        "created_at": latest_created_at,
        "proposals": latest_proposals,
        "eligible_exports": None if exported_ids is None else len(exported_ids),
        "exported_proposal_ids": exported_ids,
    }

    # Validation: regime + snapshot presence.
    # "no data yet" and "the query broke" must be DISTINGUISHABLE, so a failure
    # is recorded rather than silently reported as an absent regime/snapshot.
    validation = {}
    try:
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
            except Exception as exc:  # noqa: BLE001 -- recorded, never silent
                validation["regime"] = None
                chain_errors["validation_regime"] = f"{type(exc).__name__}: {exc}"
            try:
                snap = conn.execute(
                    "SELECT snapshot_id, source_commit, created_at FROM dataset_snapshot "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                validation["snapshot"] = {
                    "snapshot_id": str(snap["snapshot_id"]) if snap else None,
                    "source_commit": str(snap["source_commit"]) if snap else None,
                }
            except Exception as exc:  # noqa: BLE001 -- recorded, never silent
                validation["snapshot"] = None
                chain_errors["validation_snapshot"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 -- recorded, never silent
        validation["regime"] = None
        validation["snapshot"] = None
        chain_errors["validation_connect"] = f"{type(exc).__name__}: {exc}"

    return {
        "generated_at": utc_now(),
        "research_status": {
            "universe_eligible": _field(refr, "securities_expected"),
            "with_price_history": _field(refr, "with_bars"),
            "stale_names": _field(refr, "stale_count"),
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
        # DURABLE vs HISTORICAL is explicit: `last_run` comes from the durable
        # decision ledger, while the research-cycle log is a HISTORICAL snapshot
        # that the async finalizer may never have refreshed. Labelling the stale
        # cycle status as the current durable decision status was the defect.
        "portfolio_status": {
            "last_run": proposal,
            "last_cycle_decision_status": _cycle_decision_status(last_cycle, chain_errors),
            "last_cycle_as_of": (last_cycle or {}).get("as_of"),
        },
        "proposal_status": {
            # Durable: the latest decision's persisted proposals and their
            # published-authority exports (None when the value is UNKNOWN).
            "latest_decision_run_id": (chain["latest_decision"] or {}).get("run_id"),
            "proposals": (chain["latest_decision"] or {}).get("proposals", 0),
            "eligible_exports": (chain["latest_decision"] or {}).get("eligible_exports", 0),
            # Historical cycle-log status only -- never the durable status.
            "last_cycle_decision_status": _cycle_decision_status(last_cycle, chain_errors),
        },
        "decision_chain": {
            "research_cycle": (last_cycle or {}).get("status"),
            **chain,
            "runner_receipts": _runner_receipts(),
        },
        "validation_forward": {
            "production_predictions": _field(fwd, "production_predictions"),
            "predictions_due": _field(fwd, "predictions_due"),
            "matured": _matured_total(fwd),
            **validation,
        },
        "report_status": {
            "daily": "enabled (Hermes cron 23:00)",
            "weekly": "enabled (Fri 23:00)",
            "broker_analytics": _path_state(Path("/var/lib/tradehub/analytics/latest.json")),
        },
    }


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    exp = ExperimentDB(research_paths().experiment_db)
    print(json.dumps(operator_status(settings=settings, experiment_db=exp), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

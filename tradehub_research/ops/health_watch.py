"""Health watch (observation mode, issue-free standing job).

Deterministic checker for the alert-worthy conditions in the owner brief
(2026-08-31). Prints ONE LINE PER ALERT; prints NOTHING when everything is
healthy (the Hermes no_agent watchdog delivers only non-empty output).
Never modifies state; never tunes anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ALERTS: list[str] = []
ACK_FILE = Path("/var/lib/tradehub-research/autonomy/acknowledged_events.json")
DUPLICATE_WINDOW_MINUTES = 60  # a scheduler overlap/restart re-fire hazard window
#: How long the watch may spend on automatic remediation before it must report.
#: Bounded recovery: the nightly job reports on schedule and the checkpoint
#: resumes next cycle instead of the watch sitting inside the provider's hourly
#: quota window (fetch_one waits up to 12 h by design).
HEALTH_WATCH_REMEDIATION_SECONDS = float(os.environ.get("TRADEHUB_WATCH_REMEDIATION_SECONDS", 900))


def _acknowledged() -> set[tuple[str, str]]:
    """Operator-acknowledged (day, as_of) duplicate-cycle events.

    Acknowledgment is a documented operator decision (e.g. an intentional
    test double-run); the cycle ledger stays append-only and untouched.
    """
    if not ACK_FILE.exists():
        return set()
    try:
        data = json.loads(ACK_FILE.read_text())
    except (ValueError, OSError):
        return set()
    return {
        (str(item.get("day", "")), str(item.get("as_of", "")))
        for item in data
        if isinstance(item, dict)
    }


def _alert(message: str) -> None:
    ALERTS.append(f"TRADEHUB WATCH: {message}")


def check_cycle_health(paths) -> None:
    """Scheduled cycle missed / duplicate cycle (M/W/F cadence)."""
    log = paths.research_dir / "cycle-log.jsonl"
    if not log.exists():
        _alert("research cycle log missing")
        return
    entries = []
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if not entries:
        _alert("no research cycle entries recorded")
        return
    last = max(entries, key=lambda e: e.get("created_at", ""))
    try:
        last_at = datetime.fromisoformat(str(last.get("created_at", "")).replace("Z", "+00:00"))
    except ValueError:
        _alert("cycle log has an unparseable created_at")
        return
    age_hours = (datetime.now(timezone.utc) - last_at).total_seconds() / 3600
    # M/W/F cadence: a healthy gap is <= ~3.5 days (Fri->Mon). 4.5 days is missed.
    if age_hours > 4.5 * 24:
        _alert(f"research cycle missed: last cycle {age_hours:.0f}h ago")
    as_of_by_day: dict[str, list] = {}
    for entry in entries:
        day = str(entry.get("created_at", ""))[:10]
        as_of = str(entry.get("as_of", ""))
        if day and as_of:
            as_of_by_day.setdefault(day, []).append((as_of, str(entry.get("created_at", ""))))
    acked = _acknowledged()
    for day, runs in as_of_by_day.items():
        # A duplicate hazard is TWO runs on the SAME day for the SAME as_of
        # within a short window (timer overlap / restart re-fire). The Monday
        # cycle legitimately re-screens Friday's as_of (no new completed
        # session over the weekend) and hours-apart re-runs are idempotent
        # re-screens -- neither is a scheduler duplicate.
        by_as_of: dict[str, list] = {}
        for as_of, created_at in runs:
            by_as_of.setdefault(as_of, []).append(created_at)
        for as_of, stamps in by_as_of.items():
            if len(stamps) < 2:
                continue
            stamps_sorted = sorted(stamps)
            gap_minutes = (
                datetime.fromisoformat(stamps_sorted[-1].replace("Z", "+00:00"))
                - datetime.fromisoformat(stamps_sorted[0].replace("Z", "+00:00"))
            ).total_seconds() / 60
            if gap_minutes > DUPLICATE_WINDOW_MINUTES:
                continue
            if (day, as_of) in acked:
                continue
            _alert(f"duplicate cycle on {day} (as_of {as_of}, {gap_minutes:.0f} min apart)")


def _incident_log(paths):
    return paths.research_dir / "freshness_incidents.jsonl"


#: Line kind for a remediation attempt recorded against an incident that is
#: already in the log. The incident line itself stays the first observation.
REMEDIATION_EVENT = "remediation_attempt"


def incident_records(paths) -> list[dict]:
    """Every durable record, in append order. Tolerant of a partial last line.

    Reads both kinds: the incident itself (``incident_id``) and the
    ``remediation_attempt`` events appended for later runs of the same incident.
    """
    path = _incident_log(paths)
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _remediation_digest(remediation: dict) -> str:
    """Stable digest of a remediation block, which is what changes per run."""
    material = json.dumps(remediation or {}, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _remediation_event(incident: str, payload: dict, records: list[dict]) -> dict | None:
    """The next append-only remediation event, or None when nothing new to say.

    Returns None when the payload carries no remediation block (a bare re-report
    has no new evidence) or when this exact remediation outcome is already logged
    anywhere for this incident -- including the incident line's own first
    observation, so re-running the watch on an unchanged incident stays a single
    record. A *changed* outcome (a later run whose ledger write failed, say) is
    always appended instead of being discarded as a duplicate incident.
    """
    remediation = payload.get("remediation")
    if not remediation:
        return None
    digest = _remediation_digest(remediation)
    related = [record for record in records if record.get("incident_id") == incident]
    seen = {
        _remediation_digest(record.get("remediation") or {})
        for record in related
        if record.get("remediation")
    }
    if digest in seen:
        return None
    attempts = [record for record in related if record.get("event") == REMEDIATION_EVENT]
    return {
        "event": REMEDIATION_EVENT,
        "incident_id": incident,
        "attempt": len(attempts) + 1,
        "remediation_digest": digest,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "remediation": remediation,
    }


def _record_incident(paths, payload: dict) -> str | None:
    """Append-only structured evidence.

    The INCIDENT line is idempotent per ``incident_id``: the same expected-session
    / ticker set is one incident and re-running the watch must not duplicate it.

    Remediation evidence is deliberately *not* part of that identity. A later run
    can repair the same symbol again and land in a different state -- most
    importantly a repair whose success row could not be appended to the ledger --
    and dropping that payload because the incident id already existed left the
    durable record asserting the earlier, cleaner state. So a changed remediation
    block is appended as its own ``remediation_attempt`` event: history is never
    mutated away, and the later failure stays discoverable.

    Returns None on success, or a human-readable reason when the log could not be
    written. The caller puts that reason into the report: a failure to record
    incident evidence must be visible, not silent. Only expected I/O failures are
    caught -- a programming fault propagates.
    """
    path = _incident_log(paths)
    incident = payload.get("incident_id")
    try:
        records = incident_records(paths) if path.exists() else []
        existing = [
            record for record in records if incident and record.get("incident_id") == incident
        ]
        line = payload
        if any(record.get("event") != REMEDIATION_EVENT for record in existing):
            line = _remediation_event(str(incident), payload, records)
            if line is None:
                return None  # the incident, and this exact attempt, are recorded
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True) + "\n")
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def _symbol_capacity_state(settings) -> dict:
    """Rolling-month distinct-symbol capacity, read-only.

    Reported every run so the licence ceiling is visible long before it binds --
    the fleet sat at 444/450 unnoticed because nothing surfaced it.

    Only expected I/O failures are tolerated here; a programming fault
    propagates, because a silently empty capacity block would read as "no
    constraint" exactly when the constraint matters.
    """
    import sqlite3

    from tradehub_research.adapters.tiingo import TiingoQuota
    from tradehub_research.ops.symbol_capacity import capacity_report

    quota = TiingoQuota(state_path=settings.adapter_cache_dir / "tiingo-operational.sqlite")
    try:
        return capacity_report(quota, now=time.time())
    except (sqlite3.Error, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def reconciliation(audit, after, summary) -> dict:
    """The reconciled fleet snapshot.

    Both audits are the authority for what IS true (they read the persisted
    database); the summary says what the run CLAIMS. Three transitions move a
    security out of the stale set, and all three are needed or the arithmetic
    drifts:

      * repaired            -- a fetch landed the expected session
                               (observed as the rise in ``at_expected_session``)
      * relieved_into_window-- a fetch advanced the symbol but it is still inside
                               the documented rolling window, so it is no longer
                               materially stale (observed as the rise in
                               ``within_rolling_window``)
      * newly_classified_exceptions -- the symbol turned out to be delisted or
                               unfetchable (observed as the rise in exceptions)

    Every value below is OBSERVED from the two audits. The run summary's repair
    count is cross-checked against the observed rise, so "the run said 44 but the
    database shows 45" is reported instead of hidden.
    """
    at_expected_before = audit.fresh
    at_expected_after = after.fresh
    within_before = len(audit.lagging_within_window)
    within_after = len(after.lagging_within_window)

    repaired_observed = at_expected_after - at_expected_before
    relieved_into_window = within_after - within_before
    newly_exceptions = after.excluded_exceptions - audit.excluded_exceptions

    return {
        "universe_total": audit.universe_total,
        "excluded_exceptions": audit.excluded_exceptions,
        "eligible": audit.eligible,
        "fresh_before": audit.fresh_before,
        "at_expected_session": at_expected_before,
        "within_rolling_window": within_before,
        "stale_before": audit.stale_before,
        # observed transitions
        "repaired_this_run": repaired_observed,
        "reported_repaired": int(summary.get("repaired", 0) or 0),
        "relieved_into_window": relieved_into_window,
        "newly_classified_exceptions": newly_exceptions,
        # after-state
        "fresh_after": after.fresh_before,
        "at_expected_after": at_expected_after,
        "within_rolling_window_after": within_after,
        "stale_after": after.stale_before,
        "quarantined_after": after.stale_count,
        "eligible_after": after.eligible,
        "excluded_exceptions_after": after.excluded_exceptions,
        "quota_blocked": bool(summary.get("quota_blocked")),
        "time_budget_exhausted": bool(summary.get("time_budget_exhausted")),
    }


def _reconcile_problems(rec: dict) -> list[str]:
    """Invariants that must hold for a rendered report to be trustworthy."""
    problems: list[str] = []
    # every audit must be internally consistent
    if rec["universe_total"] != rec["eligible"] + rec["excluded_exceptions"]:
        problems.append("universe_total != eligible + excluded_exceptions")
    if rec["eligible"] != rec["fresh_before"] + rec["stale_before"]:
        problems.append("eligible != fresh_before + stale_before")
    if rec["fresh_before"] != rec["at_expected_session"] + rec["within_rolling_window"]:
        problems.append("fresh_before != at_expected + within_window")
    if rec.get("eligible_after") is not None:
        if rec["eligible_after"] != rec["fresh_after"] + rec["stale_after"]:
            problems.append("eligible_after != fresh_after + stale_after")
        if rec["eligible_after"] != (rec["eligible"] - rec["newly_classified_exceptions"]):
            problems.append("eligible_after != eligible - newly_classified_exceptions")
    if rec["fresh_after"] != (rec["at_expected_after"] + rec["within_rolling_window_after"]):
        problems.append("fresh_after != at_expected_after + within_window_after")
    # the run's claim must match what the database shows
    if rec["reported_repaired"] != rec["repaired_this_run"]:
        problems.append(
            f"the run reported {rec['reported_repaired']} repairs but the database "
            f"shows {rec['repaired_this_run']} (repairs did not land, or the "
            f"re-audit disagrees)"
        )
    # the three transitions must fully account for the fall in the stale count
    if rec["stale_after"] != (
        rec["stale_before"]
        - rec["repaired_this_run"]
        - rec["relieved_into_window"]
        - rec["newly_classified_exceptions"]
    ):
        problems.append(
            "stale_after != stale_before - repaired - relieved_into_window "
            "- newly_classified_exceptions (stale securities are unaccounted for)"
        )
    if rec["stale_after"] < 0 or rec["fresh_after"] < 0:
        problems.append("a reconciled count went negative")
    if rec["quarantined_after"] != rec["stale_after"]:
        # The quarantine must mirror the stale set exactly: a stale name that is
        # not quarantined could feed downstream signals.
        problems.append(
            f"quarantined_after({rec['quarantined_after']}) != "
            f"stale_after({rec['stale_after']}) (quarantine would not match the stale set)"
        )
    return problems


def render_freshness_report(audit, after, summary, residual, capacity=None) -> list[str]:
    """The operator-facing report. Pure: no I/O, so its shape is testable.

    Every count is named for exactly one thing and the arithmetic reconciles, so
    the report reads as a fleet snapshot rather than a set of unrelated numbers.
    If the counts do not reconcile -- or the quarantine does not match the stale
    set -- the report says so loudly instead of rendering a plausible-looking
    summary.
    """
    if not audit.stale:
        return []  # no incident at all: the watch stays silent

    rec = reconciliation(audit, after, summary)
    rec["quarantined_after"] = residual.get("active", after.stale_count)
    problems = _reconcile_problems(rec) + list(audit.invariant_errors())

    if not after.stale:
        lines = [
            "TRADEHUB WATCH — AUTO-RECOVERED",
            f"Expected session: {audit.expected_session}",
            f"Universe total: {rec['universe_total']:,}",
            f"Excluded legitimate exceptions: {rec['excluded_exceptions']:,}",
            f"Eligible: {rec['eligible']:,}",
            f"Stale before remediation: {rec['stale_before']:,}",
            f"Repaired this run: {rec['repaired_this_run']:,}",
            f"Advanced into the rolling window: {rec['relieved_into_window']:,}",
            f"Newly classified exceptions: {rec['newly_classified_exceptions']:,}",
            f"Fresh after remediation: {rec['fresh_after']:,}",
            "Stale after remediation: 0",
            "Quarantined after remediation: 0",
            "Downstream signals: healthy",
        ]
        if summary.get("repair_ledger_unrecorded"):
            # Disclosed even on the happy path: the recovery is real but its
            # evidence is not, and the failure-streak reader trusts that ledger.
            lines.append(
                f"{summary['repair_ledger_unrecorded']:,} repair(s) could not be recorded in the "
                "attempt ledger; the failure-streak reader trusts that ledger, so those symbols "
                "may keep reading as failing until it is writable"
            )
        if problems:
            lines.append("")
            lines.append("REPORT INTEGRITY ERROR (counts do not reconcile):")
            lines.extend(f"- {p}" for p in problems)
        return lines

    lines = [
        "TRADEHUB WATCH — DATA FRESHNESS DEGRADED",
        f"Expected session: {audit.expected_session}",
        "",
        f"Universe total: {rec['universe_total']:,}",
        f"Excluded legitimate exceptions: {rec['excluded_exceptions']:,}",
        f"Eligible: {rec['eligible']:,}",
        f"  fresh before remediation: {rec['fresh_before']:,}"
        f"  (at expected session {rec['at_expected_session']:,},"
        f" within rolling window {rec['within_rolling_window']:,})",
        f"  stale before remediation: {rec['stale_before']:,}",
        "",
        "Automatic remediation:",
        f"- repaired this run: {rec['repaired_this_run']:,}",
        f"- advanced into the rolling window: {rec['relieved_into_window']:,}",
        f"- newly classified exceptions: {rec['newly_classified_exceptions']:,}",
        "",
        f"Fresh after remediation: {rec['fresh_after']:,}",
        f"Stale after remediation: {rec['stale_after']:,}",
        f"Quarantined after remediation: {rec['quarantined_after']:,}",
        "",
        "Root causes:",
    ]
    for cause, members in sorted(audit.groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {len(members):,} {cause.replace('_', ' ').lower()}")
    if audit.scheduled_count:
        run = audit.refresh_run or {}
        lines.append("")
        lines.append("Scheduled deferral (bounded by design, not an interruption):")
        lines.append(
            f"- {audit.scheduled_count:,} securities deferred by the COMPLETED "
            f"{audit.expected_session} refresh"
        )
        lines.append(
            f"- that run served {run.get('refreshed') or 0:,} of "
            f"{run.get('candidates') or 0:,} candidates against a "
            f"{run.get('rotation_budget') or 0:,}-request budget"
        )
        lines.append("- the rotation drains them on later runs; not remediated here")
    oldest = min((s.last_bar for s in after.stale if s.last_bar), default=None)
    if oldest:
        lines.append(f"Oldest unresolved data: {oldest}")
    lines.append("")
    lines.append("Downstream protection:")
    lines.append(f"- {rec['quarantined_after']:,} securities marked DATA_STALE")
    lines.append("- excluded from affected signals")
    if rec["quota_blocked"]:
        lines.append("- remediation paused on the provider quota reserve (resumes next cycle)")
    if summary.get("repair_ledger_unrecorded"):
        lines.append(
            f"- {summary['repair_ledger_unrecorded']:,} repair(s) could not be recorded in the "
            "attempt ledger; the failure-streak reader trusts that ledger, so those symbols may "
            "keep reading as failing until it is writable"
        )
    if rec["time_budget_exhausted"]:
        lines.append("- remediation hit its time budget (resumes next cycle)")
    if capacity:
        lines.append("")
        lines.append("Rolling-month symbol capacity:")
        lines.append(
            f"- {capacity.get('used', 0):,}/{capacity.get('limit', 0):,} distinct symbols reserved"
            f"  (headroom {capacity.get('headroom', 0):,})"
        )
        if capacity.get("at_capacity"):
            lines.append("- AT CAPACITY: only already-reserved symbols can be refreshed")
        if capacity.get("deferred"):
            lines.append(f"- {capacity['deferred']:,} new symbols could not be admitted")
    lines.append("")
    lines.append("Status: NEEDS ATTENTION")
    examples = after.stale[:8]
    if examples:
        lines.append("")
        lines.append("Unresolved examples:")
        for s in examples:
            lines.append(
                f"{s.ticker} — {s.last_bar or 'no bars'} — {s.classification} — "
                f"{s.last_attempt_at or 'not attempted'}"
            )
    if problems:
        lines.append("")
        lines.append("REPORT INTEGRITY ERROR (counts do not reconcile):")
        lines.extend(f"- {p}" for p in problems)
    return lines


def check_data_freshness(settings, paths) -> None:
    """Diagnose -> remediate -> verify -> protect downstream -> report.

    Replaces the passive "N securities stale" alert. Emits a report only after
    remediation has run (or when the condition needs a human); stays silent when
    there is nothing wrong.
    """
    from tradehub_research.ops import data_freshness as df
    from tradehub_research.ops.downstream_guard import sync_quarantine
    from tradehub_research.validation.experiment_db import ExperimentDB

    experiment_db = ExperimentDB(paths.experiment_db)
    audit = df.audit_universe(settings=settings, paths=paths, experiment_db=experiment_db)
    capacity = _symbol_capacity_state(settings)

    quarantined = sync_quarantine(
        [s.__dict__ for s in audit.stale],
        expected_session=audit.expected_session,
        research_dir=paths.research_dir,
    )

    if not audit.stale:
        if quarantined["cleared"]:
            _alert(
                "DATA FRESHNESS RECOVERED: "
                f"{len(quarantined['cleared'])} securities returned to downstream signals"
            )
        return  # healthy: silent (quarantine reconciliation already happened)

    summary = df.remediate(
        settings=settings,
        paths=paths,
        experiment_db=experiment_db,
        audit=audit,
        time_budget_seconds=HEALTH_WATCH_REMEDIATION_SECONDS,
    )
    verification = df.verify(settings=settings, paths=paths, run_key=summary["run_key"])
    after = df.audit_universe(settings=settings, paths=paths, experiment_db=experiment_db)
    residual = sync_quarantine(
        [s.__dict__ for s in after.stale],
        expected_session=after.expected_session,
        research_dir=paths.research_dir,
    )

    oldest = min((s.last_bar for s in after.stale if s.last_bar), default=None)
    evidence_problem = _record_incident(
        paths,
        {
            "incident_id": df.incident_id(audit.expected_session, [s.ticker for s in audit.stale]),
            "expected_session": audit.expected_session,
            "detected_at": audit.generated_at,
            "universe": audit.universe,
            "fresh": audit.fresh,
            "initially_stale": audit.stale_count,
            "scheduled_deferrals": audit.scheduled_count,
            "refresh_run": audit.refresh_run,
            "root_causes": {k: len(v) for k, v in sorted(audit.groups.items())},
            "remediation": {
                "run_key": summary["run_key"],
                "targeted": summary["targeted"],
                "scheduled_deferrals": summary.get("scheduled_deferrals", 0),
                # A repair whose ledger evidence could not be written: disclosed,
                # because the failure-streak reader trusts that ledger and would
                # otherwise put the symbol back into cooling with no explanation.
                "repair_ledger_unrecorded": summary.get("repair_ledger_unrecorded", 0),
                "repaired": summary["repaired"],
                "excluded": summary["excluded"],
                "unresolved": summary["unresolved"],
                "attempts": summary["attempts"],
                "quota_blocked": summary["quota_blocked"],
            },
            "verification": {
                "repaired_verified": verification["repaired_verified"],
                "checkpoint_consistent": verification["checkpoint_consistent"],
                "duplicate_symbols": verification["duplicate_symbols"],
            },
            "remaining_stale": after.stale_count,
            "oldest_unresolved": oldest,
            "downstream": {"quarantined_active": residual["active"]},
            "symbol_capacity": capacity,
            "disposition": "NEEDS_ATTENTION" if after.stale_count else "AUTO_RECOVERED",
            "unresolved_examples": [
                {
                    "ticker": s.ticker,
                    "last_bar": s.last_bar,
                    "classification": s.classification,
                    "attempted": bool(s.last_attempt_at),
                }
                for s in after.stale[:8]
            ],
        },
    )
    ALERTS.extend(render_freshness_report(audit, after, summary, residual, capacity))
    if evidence_problem:
        # Not silent: the report itself says the evidence record is missing.
        ALERTS.append("")
        ALERTS.append(f"WARNING: incident evidence not recorded ({evidence_problem})")


def check_forward_ledger(experiment_db, paths) -> None:
    """Prediction dedupe failure / maturation failure."""
    from tradehub_research.ops.health import forward_health

    fwd = forward_health(experiment_db=experiment_db, paths=paths)
    due = fwd.get("predictions_due", 0)
    if due > 0:
        _alert(f"{due} forward predictions due with no outcome (maturation backlog)")


def check_paper_proof_and_kill_switch() -> None:
    """PAPER proof failure / kill-switch unexpected state / unexpected order."""
    import os

    token = os.getenv("TRADEHUB_AUTONOMY_TOKEN")
    api = os.getenv("TRADEHUB_EXECUTION_API", "http://127.0.0.1:8787")
    if token:
        import httpx

        try:
            proof = httpx.get(
                f"{api}/account/proof",
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            ).json()
            if proof.get("account_type") != "PAPER" or not proof.get("assets_ok"):
                _alert(
                    "PAPER proof failed: "
                    f"{proof.get('account_type')} assets_ok={proof.get('assets_ok')}"
                )
        except Exception as exc:  # noqa: BLE001 -- watch must not crash
            _alert(f"PAPER proof unreachable: {type(exc).__name__}")
    switch = Path("/var/lib/tradehub/autonomy/kill_switch")
    if switch.exists():
        try:
            content = switch.read_text().strip().upper()
        except PermissionError:
            # Expected under research/runtime isolation: the execution-owned
            # kill switch is enforced again at the execution boundary.
            content = None
        if content is not None and content not in ("BLOCKED", "CLEARED", ""):
            _alert(f"kill-switch file has unexpected content: {content!r}")
    ledger = Path("/var/lib/tradehub-research/autonomy/paper_run_ledger.jsonl")
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("submitted") and not entry.get("dry_run"):
                _alert(f"real (non-dry-run) autonomous order recorded: {entry.get('proposal_id')}")


def check_services() -> None:
    """Service restart loop detection."""
    import subprocess

    for unit in ("tradehub-execution.service", "tradehub-research.service"):
        try:
            out = subprocess.run(
                ["systemctl", "show", unit, "-p", "NRestarts", "-p", "ActiveState"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            nrestarts = next(
                (
                    line.split("=", 1)[1]
                    for line in out.splitlines()
                    if line.startswith("NRestarts=")
                ),
                "?",
            )
            active = next(
                (
                    line.split("=", 1)[1]
                    for line in out.splitlines()
                    if line.startswith("ActiveState=")
                ),
                "?",
            )
            if active != "active":
                _alert(f"{unit} not active ({active})")
            elif nrestarts not in ("?", "0") and int(nrestarts) >= 5:
                _alert(f"{unit} restart loop ({nrestarts} restarts)")
        except Exception:  # noqa: BLE001
            _alert(f"{unit} state unreadable")


def check_reconciliation() -> None:
    """Broker reconciliation health via the research-readable sanitized handoff."""
    handoff = Path("/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.json")
    if not handoff.exists():
        _alert("no sanitized broker handoff (reconciliation never ran)")
        return
    try:
        row = json.loads(handoff.read_text())
        observed = datetime.fromisoformat(str(row.get("as_of", "")).replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - observed).total_seconds() / 3600
        if age_hours > 60:
            _alert(f"sanitized broker handoff {age_hours:.0f}h old (reconciliation stale)")
    except (ValueError, OSError):
        _alert("sanitized broker handoff unreadable")


#: A genuine committee queue is driven within days of issue (the research cycle
#: runs M/W/F and the model worker follows it). A run with no score this long
#: after it was issued is a stalled decision plane, not a busy one.
DECISION_STALL_HOURS = 72.0


def _parse_utc(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decision_plane_state(research_db, *, now: datetime | None = None) -> dict:
    """Outstanding committee work versus the decision gate (read-only).

    "Outstanding" is a GENUINE committee run that produced no ``score_snapshot``
    -- the exact condition ``decision_pipeline`` reports as BLOCKED_NO_VALID_SCORE,
    so the watch cannot disagree with the gate. Acceptance runs are excluded: they
    are historical fixtures, not the production queue.

    Live, this is the gap that let the paper loop sit idle for ten days while the
    watch stayed silent: every other condition covered an INPUT to the decision,
    none covered the decision itself.
    """
    from tradehub_research.ops.acceptance_rows import genuine_clause

    now = now or datetime.now(timezone.utc)
    predicate, params = genuine_clause("c.pipeline_run_id")
    with research_db.connect(read_only=True) as conn:
        outstanding = conn.execute(
            "SELECT c.created_at FROM committee_run c "
            f"WHERE {predicate} AND NOT EXISTS ("
            "  SELECT 1 FROM score_snapshot s WHERE s.committee_run_id=c.committee_run_id)",
            params,
        ).fetchall()
        latest_predicate, latest_params = genuine_clause("p.run_id")
        latest = conn.execute(
            f"SELECT p.run_id, p.as_of FROM pipeline_run p WHERE {latest_predicate} "
            "ORDER BY p.as_of DESC LIMIT 1",
            latest_params,
        ).fetchone()
        latest_as_of = None
        candidates = scored = 0
        if latest is not None:
            latest_as_of = latest["as_of"]
            candidates = conn.execute(
                "SELECT count(*) FROM candidate WHERE run_id=?", (latest["run_id"],)
            ).fetchone()[0]
            scored = conn.execute(
                "SELECT count(*) FROM score_snapshot s JOIN committee_run c "
                "ON c.committee_run_id=s.committee_run_id WHERE c.pipeline_run_id=?",
                (latest["run_id"],),
            ).fetchone()[0]
        proposals = conn.execute("SELECT count(*) FROM trade_proposal").fetchone()[0]

    ages = [
        (at, (now - at).total_seconds() / 3600)
        for at in (_parse_utc(row["created_at"]) for row in outstanding)
        if at is not None
    ]
    oldest_at, oldest_hours = (None, 0.0)
    if ages:
        oldest_at, oldest_hours = max(ages, key=lambda item: item[1])
    return {
        "outstanding_runs": len(outstanding),
        "oldest_outstanding_at": (
            oldest_at.isoformat().replace("+00:00", "Z") if oldest_at is not None else None
        ),
        "oldest_outstanding_hours": oldest_hours,
        "latest_pipeline_as_of": latest_as_of,
        "latest_pipeline_candidates": candidates,
        "latest_pipeline_scored": scored,
        "proposals_lifetime": proposals,
    }


def check_decision_plane(research_db, *, now: datetime | None = None) -> None:
    """The decision plane: work issued but never scored, decisions blocked."""
    import sqlite3

    try:
        state = decision_plane_state(research_db, now=now)
    except (sqlite3.Error, OSError) as exc:
        _alert(f"decision-plane state unreadable ({type(exc).__name__})")
        return
    if not state["outstanding_runs"] or state["oldest_outstanding_hours"] < DECISION_STALL_HOURS:
        return
    if state["latest_pipeline_as_of"]:
        cycle = (
            f"{state['latest_pipeline_scored']}/{state['latest_pipeline_candidates']} "
            f"candidates scored on the {str(state['latest_pipeline_as_of'])[:10]} cycle"
        )
    else:
        cycle = "no pipeline run recorded"
    _alert(
        f"decision plane stalled: {state['outstanding_runs']} committee run(s) without a score, "
        f"oldest {str(state['oldest_outstanding_at'])[:10]} "
        f"({state['oldest_outstanding_hours']:.0f}h); {cycle}; "
        f"{state['proposals_lifetime']} proposal(s) ever"
    )


def main() -> int:
    from tradehub_research.config import ResearchSettings
    from tradehub_research.db import ResearchDB
    from tradehub_research.ops.common import research_paths
    from tradehub_research.validation.experiment_db import ExperimentDB

    paths = research_paths()
    settings = ResearchSettings()
    exp = ExperimentDB(paths.experiment_db)
    check_cycle_health(paths)
    check_data_freshness(settings, paths)
    check_forward_ledger(exp, paths)
    check_decision_plane(ResearchDB(paths.research_db, settings.busy_timeout_ms))
    check_paper_proof_and_kill_switch()
    check_services()
    check_reconciliation()
    if ALERTS:
        print("\n".join(ALERTS))
    # Exit 0 whenever the watch itself ran: alerts are the stdout deliverable
    # (the Hermes no_agent cron delivers non-empty output). A non-zero exit is
    # reserved for genuine script failures so a healthy-but-alerting watch is
    # never reported as a broken job.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

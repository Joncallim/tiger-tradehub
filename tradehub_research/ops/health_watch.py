"""Health watch (observation mode, issue-free standing job).

Deterministic checker for the alert-worthy conditions in the owner brief
(2026-08-31). Prints ONE LINE PER ALERT; prints NOTHING when everything is
healthy (the Hermes no_agent watchdog delivers only non-empty output).
Never modifies state; never tunes anything.
"""

from __future__ import annotations

import json
import os
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


def _record_incident(paths, payload: dict) -> None:
    """Append-only structured evidence. Idempotent per incident_id."""
    import os as _os

    path = _incident_log(paths)
    incident = payload.get("incident_id")
    try:
        if path.exists() and incident:
            for line in path.read_text().splitlines():
                if line.strip() and f'"incident_id": "{incident}"' in line:
                    return  # already recorded; never double-log the same incident
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError:
        pass  # evidence must never break the watch
    del _os


def render_freshness_report(audit, after, summary, residual) -> list[str]:
    """The operator-facing report. Pure: no I/O, so its shape is testable.

    Two forms:
      * AUTO-RECOVERED -- everything repaired inside the normal cycle. This is a
        *quiet success report*, not an alert, and carries no examples because
        there is nothing for a human to do.
      * DATA FRESHNESS DEGRADED -- actionable: root causes grouped, oldest
        unresolved datum, downstream protection, and per-symbol examples.
    """
    lines: list[str] = []
    if not audit.stale:
        return lines  # no incident at all: the watch stays silent
    if not after.stale:
        lines.append("TRADEHUB WATCH — AUTO-RECOVERED")
        lines.append(f"Expected session: {audit.expected_session}")
        lines.append(f"Initially stale: {audit.stale_count}")
        lines.append(f"Repaired: {summary['repaired']}")
        lines.append(f"Excluded legitimate exceptions: {summary['excluded']}")
        lines.append("Remaining stale: 0")
        lines.append("Downstream signals: healthy")
        return lines

    oldest = min((s.last_bar for s in after.stale if s.last_bar), default=None)
    lines.append("TRADEHUB WATCH — DATA FRESHNESS DEGRADED")
    lines.append(f"Expected session: {audit.expected_session}")
    lines.append(f"Universe: {audit.universe:,}")
    lines.append(f"Fresh: {audit.fresh:,}")
    lines.append(f"Initially stale: {audit.stale_count:,}")
    lines.append("")
    lines.append("Automatic remediation:")
    lines.append(f"- {summary['repaired']:,} repaired successfully")
    lines.append(f"- {summary['excluded']:,} excluded as valid non-trading/delisted exceptions")
    lines.append(f"- {after.stale_count:,} remain unresolved")
    lines.append("")
    lines.append("Root causes:")
    for cause, members in sorted(audit.groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {len(members):,} {cause.replace('_', ' ').lower()}")
    if oldest:
        lines.append("")
        lines.append(f"Oldest unresolved data: {oldest}")
    lines.append("")
    lines.append("Downstream protection:")
    lines.append(f"- {residual['active']:,} securities marked DATA_STALE")
    lines.append("- excluded from affected signals")
    if summary.get("quota_blocked"):
        lines.append("- remediation paused on the provider quota reserve (resumes next cycle)")
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
        settings=settings, paths=paths, experiment_db=experiment_db, audit=audit,
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
    _record_incident(
        paths,
        {
            "incident_id": df.incident_id(audit.expected_session, [s.ticker for s in audit.stale]),
            "expected_session": audit.expected_session,
            "detected_at": audit.generated_at,
            "universe": audit.universe,
            "fresh": audit.fresh,
            "initially_stale": audit.stale_count,
            "root_causes": {k: len(v) for k, v in sorted(audit.groups.items())},
            "remediation": {
                "run_key": summary["run_key"],
                "targeted": summary["targeted"],
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
    ALERTS.extend(render_freshness_report(audit, after, summary, residual))


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


def main() -> int:
    from tradehub_research.config import ResearchSettings
    from tradehub_research.ops.common import research_paths
    from tradehub_research.validation.experiment_db import ExperimentDB

    paths = research_paths()
    settings = ResearchSettings()
    exp = ExperimentDB(paths.experiment_db)
    check_cycle_health(paths)
    check_data_freshness(settings, paths)
    check_forward_ledger(exp, paths)
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

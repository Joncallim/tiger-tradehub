"""Health watch (observation mode, issue-free standing job).

Deterministic checker for the alert-worthy conditions in the owner brief
(2026-08-31). Prints ONE LINE PER ALERT; prints NOTHING when everything is
healthy (the Hermes no_agent watchdog delivers only non-empty output).
Never modifies state; never tunes anything.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

ALERTS: list[str] = []
ACK_FILE = Path("/var/lib/tradehub-research/autonomy/acknowledged_events.json")
DUPLICATE_WINDOW_MINUTES = 60  # a scheduler overlap/restart re-fire hazard window


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


def check_data_freshness(settings, paths) -> None:
    """Stale evidence / failed ingestion."""
    from tradehub_research.ops.health import refresh_health

    refr = refresh_health(settings=settings, paths=paths)
    if refr.get("stale_count"):
        _alert(f"{refr['stale_count']} securities stale (behind {refr.get('as_of')})")
    if not refr.get("with_bars") and not refr.get("stale_count"):
        _alert("no market data present")


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
        content = switch.read_text().strip().upper()
        if content not in ("BLOCKED", "CLEARED", ""):
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
    """Broker reconciliation failure: no fresh sanitized snapshot."""
    latest = Path("/var/lib/tradehub/analytics/latest.json")
    if not latest.exists():
        _alert("no broker analytics snapshot (reconciliation never ran)")
        return
    try:
        row = json.loads(latest.read_text())
        age_days = (date.today() - date.fromisoformat(str(row.get("date", ""))[:10])).days
        if age_days > 2:
            _alert(f"broker analytics snapshot {age_days} days old (reconciliation stale)")
    except (ValueError, OSError):
        _alert("broker analytics snapshot unreadable")


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

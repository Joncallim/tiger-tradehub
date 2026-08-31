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


def _alert(message: str) -> None:
    ALERTS.append(f"TRADEHUB WATCH: {message}")


def check_cycle_health(paths) -> None:
    """Scheduled cycle missed / duplicate cycle (M/W/F cadence)."""
    log = paths.cycle_log
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
    as_ofs = [str(e.get("as_of", "")) for e in entries if e.get("as_of")]
    if len(as_ofs) != len(set(as_ofs)) and entries:
        _alert("duplicate cycle as_of detected (duplicate run)")


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
    return 1 if ALERTS else 0


if __name__ == "__main__":
    raise SystemExit(main())

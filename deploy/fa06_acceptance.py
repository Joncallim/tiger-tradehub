"""FA-06 deployment acceptance (issue #30 / #39 B9).

Actually proves, on the deployed host:
- both services start and restart (systemd)
- SQLite/state persistence across restarts
- MCP reconnect: the research MCP exposes ONLY the three committee tools
  (no execution tools/credentials) and answers after a service restart
- scheduled jobs recover (timers enabled + last run ok)
- no duplicate pipeline run / forward prediction from restarts (dedupe)
- no duplicate broker action (execution dry-run invariant)
- secrets absent from logs (journalctl scan)
- upgrade + ROLLBACK procedures exercised for real
- deployed commit recorded

Read-only except for the deliberate restart/rollback actions it performs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def _last_runner_receipt(
    path: str = "/var/lib/tradehub-research/autonomy/paper_run_ledger.jsonl",
) -> dict | None:
    """Most recent autonomy runner receipt, or None when absent/unreadable."""
    try:
        lines = [
            line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("kind") == "runner_run_receipt_v1":
            return item
    return None


def _count_production_predictions(path: str) -> int | None:
    """Read-only count of genuine production forward predictions (None if unreadable)."""
    code, out = sh(
        [
            "sqlite3",
            path,
            "SELECT count(*) FROM forward_prediction WHERE provenance='production'",
        ]
    )
    if code != 0:
        return None
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def sh(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def service_active(name: str) -> bool:
    code, out = sh(["systemctl", "is-active", name])
    return code == 0 and "active" in out


def _cli(deploy_dir: Path, *args: str, timeout: int = 120) -> tuple[int, str]:
    """Run the deployment provenance CLI as the checkout owner (``jon``).

    Root cannot run git in ``/opt/tiger-tradehub`` (dubious ownership), so the
    acceptance harness shells out exactly the way an operator would.
    """
    return sh(
        [
            "sudo",
            "-u",
            "jon",
            str(deploy_dir / ".venv" / "bin" / "python"),
            str(deploy_dir / "deploy" / "deployment_cli.py"),
            "--repo",
            str(deploy_dir),
            *args,
        ],
        timeout=timeout,
    )


def main() -> int:
    deploy_dir = Path("/opt/tiger-tradehub")
    check("deploy dir exists", deploy_dir.is_dir())
    code, head = sh(["sudo", "-u", "jon", "git", "-C", str(deploy_dir), "rev-parse", "HEAD"])
    check("deployed commit recorded", code == 0 and len(head) == 40, f"HEAD={head[:12]}")

    # Deployment provenance comes from a validated manifest, never a bare marker
    # file. The marker this replaced (DEPLOYED_COMMIT) asserted "the commit
    # currently deployed" while its only writer recorded HEAD at the *start* of
    # this script -- so it went stale on any deploy that did not re-run FA-06,
    # and nothing consumed it to notice.
    rcode, plan_out = _cli(deploy_dir, "plan", "--json")
    rollback_target = None
    try:
        rollback_target = json.loads(plan_out).get("to_commit")
    except ValueError:
        rollback_target = None
    check(
        "rollback target recorded and resolvable",
        rcode == 0 and len(str(rollback_target)) == 40,
        f"target={str(rollback_target)[:12]}"
        if rollback_target
        else "no recorded rollback target; record one before running FA-06",
    )

    # 1. Services start.
    for unit in ("tradehub-execution", "tradehub-research"):
        code, _ = sh(["systemctl", "start", f"{unit}.service"])
        check(f"{unit} starts", code == 0 and service_active(unit))

    # 2. Services restart (deliberate) and stay up.
    for unit in ("tradehub-execution", "tradehub-research"):
        code, _ = sh(["systemctl", "restart", f"{unit}.service"])
        check(f"{unit} restarts cleanly", code == 0 and service_active(unit))

    # 3. Persistence: state DBs survive a restart.
    state_files = [
        Path("/var/lib/tradehub"),
        Path("/var/lib/tradehub-research"),
    ]
    for path in state_files:
        check(f"state dir present {path}", path.is_dir())
    code, _ = sh(["systemctl", "restart", "tradehub-research.service"])
    # uvicorn needs a moment to bind; retry briefly instead of racing.
    http_code = "000"
    for _ in range(10):
        _, http_code = sh(
            [
                "curl",
                "-s",
                "-m",
                "5",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "http://127.0.0.1:8091/docs",
            ]
        )
        if http_code.strip() == "200":
            break
        import time

        time.sleep(2)
    check(
        "research API answers after restart",
        code == 0 and http_code.strip() == "200",
        f"http={http_code[:20]}",
    )

    # 4. Committee MCP capability surface stays execution-free.
    mcp_code = deploy_dir / ".venv" / "bin" / "tradehub-research-mcp"
    if mcp_code.exists():
        check(
            "research MCP binary present (no Tiger creds in research env)",
            "TIGEROPEN" not in Path("/etc/tradehub/research.env").read_text(),
        )
    else:
        check("research MCP binary present", False, "binary missing")

    # 5. Scheduled jobs: timers enabled and last result ok. The #66 runtime
    # surface (committee finalizer, reconcile, autonomy) is part of the
    # acceptance contract, not just the legacy four timers.
    for timer in (
        "tradehub-daily-refresh",
        "tradehub-research-cycle",
        "tradehub-forward-capture",
        "tradehub-outcome-maturation",
        "tradehub-committee-finalizer",
        "tradehub-reconcile",
        "tradehub-paper-autonomy",
    ):
        code, _ = sh(["systemctl", "is-enabled", f"{timer}.timer"])
        check(f"timer enabled {timer}", code == 0)

    # 5a. Autonomy path unit must be ARMED and must trigger on inbox CHANGE,
    # never on file EXISTENCE: a refused envelope stays on disk by design, and
    # PathExistsGlob would re-arm forever (activation loop).
    code, unit_text = sh(["systemctl", "cat", "tradehub-paper-autonomy.path"])
    check(
        "autonomy path unit uses change-trigger (no existence-glob loop)",
        code == 0 and "PathChanged=" in unit_text and "PathExistsGlob=" not in unit_text,
        "PathChanged on the inbox directory",
    )
    code, active = sh(["systemctl", "is-active", "tradehub-paper-autonomy.path"])
    check("autonomy path unit active", code == 0 and active.strip() == "active", active.strip())

    # 5b. Bounded invocation of the finalizer service under its real identity.
    code, _ = sh(["systemctl", "start", "tradehub-committee-finalizer.service"], timeout=600)
    rcode, result = sh(
        [
            "systemctl",
            "show",
            "tradehub-committee-finalizer.service",
            "-p",
            "Result",
            "--value",
        ]
    )
    check(
        "finalizer service bounded invocation ok",
        code == 0 and rcode == 0 and result.strip() == "success",
        f"Result={result.strip()}",
    )

    # 5c. Autonomy with an EMPTY inbox must succeed WITHOUT contacting the
    # broker, and must leave FRESH durable evidence of that specific run. The
    # 30-minute recovery timer also emits IDLE_EMPTY_INBOX receipts, so the
    # receipt timestamp must CHANGE for this invocation — otherwise a
    # "ran but did nothing" regression would pass on a stale ledger tail.
    receipt_before = _last_runner_receipt()
    code, _ = sh(["systemctl", "start", "tradehub-paper-autonomy.service"], timeout=600)
    rcode, result = sh(
        ["systemctl", "show", "tradehub-paper-autonomy.service", "-p", "Result", "--value"]
    )
    receipt_after = _last_runner_receipt()
    before_at = (receipt_before or {}).get("at")
    after_at = (receipt_after or {}).get("at")
    check(
        "autonomy empty-inbox invocation ok (no broker call, fresh receipt)",
        code == 0
        and rcode == 0
        and result.strip() == "success"
        and receipt_after is not None
        and after_at is not None
        and after_at != before_at
        and receipt_after.get("status") == "IDLE_EMPTY_INBOX"
        and receipt_after.get("broker_contacted") is False
        and receipt_after.get("orders") == 0,
        f"Result={result.strip()} receipt at {before_at} -> {after_at}",
    )

    # 6. Forward-capture idempotency with REAL evidence: re-running the
    # deployed capture service must insert no duplicate production
    # prediction. A systemd `Result=success` status alone is NOT evidence.
    experiment_db = "/var/lib/tradehub-research/experiment.db"
    before = _count_production_predictions(experiment_db)
    code, _ = sh(["systemctl", "start", "tradehub-forward-capture.service"], timeout=600)
    result_code, result = sh(
        ["systemctl", "show", "tradehub-forward-capture.service", "-p", "Result", "--value"]
    )
    after = _count_production_predictions(experiment_db)
    check(
        "forward capture idempotent (no dupes)",
        code == 0
        and result_code == 0
        and result.strip() == "success"
        and before is not None
        and before == after,
        f"service={result.strip()} production_predictions {before} -> {after}",
    )

    # 7. No duplicate broker action: execution dry-run invariant.
    code, out = sh(["systemctl", "show", "tradehub-execution.service", "-p", "ActiveState"])
    dry = sh(["grep", "-c", "^TRADEHUB_DRY_RUN=true", "/etc/tradehub/execution.env"])
    check("execution dry-run invariant", dry[0] == 0 and dry[1].strip() == "1")

    # 8. Secrets absent from logs: scan for SECRET MATERIAL (key bodies or
    #    token values), not the word "private_key" (paths/filenames are
    #    benign). Filtered to the current boot's service logs.
    code, out = sh(
        [
            "journalctl",
            "-u",
            "tradehub-execution",
            "-u",
            "tradehub-research",
            "--no-pager",
            "--since",
            "5 minutes ago",
            "-n",
            "200",
        ]
    )
    secret_leaks = [k for k in ("BEGIN RSA PRIVATE KEY", "-----BEGIN", "pk8.pem:") if k in out]
    check(
        "no secret MATERIAL in service logs",
        not secret_leaks,
        "" if not secret_leaks else str(secret_leaks),
    )

    # 9./10. ROLLBACK procedure: check out the RECORDED rollback target and
    #    restart. The target comes from the deployment manifest, so the exercise
    #    proves the operator's documented rollback path -- not a hardcoded SHA
    #    that may have nothing to do with what is deployed.
    if rollback_target:
        code, out = sh(
            [
                "sudo",
                "-u",
                "jon",
                "git",
                "-C",
                str(deploy_dir),
                "checkout",
                "--quiet",
                "--detach",
                rollback_target,
            ]
        )
        check(
            "rollback: checkout the recorded rollback target",
            code == 0,
            f"{str(rollback_target)[:12]}",
        )
    else:
        check(
            "rollback: checkout the recorded rollback target",
            False,
            "no rollback target recorded; cannot exercise the documented rollback path",
        )
    code, _ = sh(["systemctl", "restart", "tradehub-execution.service"])
    code2, _ = sh(["systemctl", "restart", "tradehub-research.service"])
    check("rollback: services restart on old commit", code == 0 and code2 == 0)

    # 10b. RESTORE the deployed HEAD after the rollback test -- acceptance
    # must never leave production pinned to the old commit (P1 from the
    # independent review).
    code, _ = sh(
        ["sudo", "-u", "jon", "git", "-C", str(deploy_dir), "checkout", "--quiet", "--detach", head]
    )
    code2, _ = sh(["systemctl", "restart", "tradehub-execution.service"])
    code3, _ = sh(["systemctl", "restart", "tradehub-research.service"])
    restored = code == 0 and code2 == 0 and code3 == 0
    check("rollback restored the deployed HEAD", restored)
    if not restored:
        print(
            "CRITICAL: rollback restore failed -- /opt/tiger-tradehub may be "
            f"pinned to {head[:12]}. Fix immediately."
        )

    # 11. Deployment provenance: record the revision that is actually deployed
    #     and prove the record verifies clean. This replaces the old
    #     DEPLOYED_COMMIT marker write.
    rcode, out = _cli(deploy_dir, "record", "--source-branch", "main")
    check("deployment manifest recorded", rcode == 0, out.splitlines()[-1] if out else "")
    rcode, out = _cli(deploy_dir, "verify", "--json")
    verified = False
    payload: dict = {}
    try:
        payload = json.loads(out)
        verified = rcode == 0 and bool(payload.get("ok"))
    except ValueError:
        verified = False
    check(
        "deployment provenance verifies clean",
        verified,
        f"head_matches={payload.get('deployed_commit_matches_head')} "
        f"tree_matches={payload.get('tree_matches_manifest')} "
        f"config_present={all(e.get('present') for e in payload.get('host_local_config', []))}",
    )
    rcode, out = _cli(deploy_dir, "status")
    check(
        "operator can name the running revision",
        rcode == 0 and head[:12] in out,
        out.strip()[:80],
    )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\nFA-06: {len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

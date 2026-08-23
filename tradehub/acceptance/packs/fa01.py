"""FA-01 — Local runtime safety preflight.

Environment: local (trusted Hermes/TradeHub host, no broker write).

Proves the deployed TradeHub process starts with the expected safety
posture: loopback-only bind, bearer auth enforcement, require_approval
and dry_run asserted (not merely printed), Tiger state reported without
leaking credential values, and no configured secrets in logs/results.
"""

from __future__ import annotations

import importlib
import subprocess

from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
)
from tradehub.acceptance.service import (
    ServiceManager,
    assert_loopback_only,
    load_env_file,
)

EXPECTED_HEALTH_KEYS = {"ok", "dry_run", "tiger_configured", "require_approval"}


def build_fa01_pack() -> PackDefinition:
    env_path = REPO_ROOT / ".env"
    env = load_env_file(env_path)
    token = env.get("TRADEHUB_API_TOKEN", "")

    def commit_recorded(ctx: RunContext) -> None:
        from tradehub.acceptance.runner import capture_commit_sha

        sha = capture_commit_sha()
        if len(sha) != 40:
            raise AssertionError_(f"commit SHA malformed: {sha!r}")
        ctx.artifacts.append(f"commit={sha}")

    def import_succeeds(ctx: RunContext) -> None:
        module = importlib.import_module("tradehub")
        version = getattr(module, "__version__", None)
        if not version:
            raise AssertionError_("tradehub.__version__ missing")

    def test_gates_green(ctx: RunContext) -> None:
        # Offline-acceptable gates: pytest, ruff lint, ruff format.
        gates = [
            ([str(REPO_ROOT / ".venv/bin/python"), "-m", "pytest", "-q"], "pytest"),
            ([str(REPO_ROOT / ".venv/bin/ruff"), "check", "."], "ruff lint"),
            ([str(REPO_ROOT / ".venv/bin/ruff"), "format", "--check", "."], "ruff format"),
        ]
        for cmd, label in gates:
            result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise AssertionError_(
                    f"{label} gate failed (exit {result.returncode}): "
                    f"{result.stdout[-800:]}{result.stderr[-800:]}"
                )

    def service_starts(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        ctx.register_secret(token)
        ctx.register_secret(manager.env.get("TIGEROPEN_TIGER_ID"))
        ctx.register_secret(manager.env.get("TIGEROPEN_ACCOUNT"))
        manager.start()
        body = manager.health()
        if not body.get("ok"):
            raise AssertionError_(f"health not ok: {body}")
        ctx.artifacts.append(ctx.write_artifact("fa01-health", {"health": body}))
        manager.stop()

    def loopback_only(ctx: RunContext) -> None:
        manager = ServiceManager(ctx, env_overrides={"TRADEHUB_BIND_HOST": "127.0.0.1"})
        manager.start()
        assert_loopback_only(manager)
        manager.stop()

    def auth_enforced(ctx: RunContext) -> None:
        import httpx

        manager = ServiceManager(ctx)
        manager.start()
        base = f"http://{manager.host}:{manager.port}"
        # valid bearer succeeds
        ok = httpx.get(
            f"{base}/health",
            headers={"Authorization": f"Bearer {manager.env.get('TRADEHUB_API_TOKEN', '')}"},
            timeout=10,
        )
        if ok.status_code != 200:
            raise AssertionError_(f"valid bearer rejected: HTTP {ok.status_code}")
        # wrong bearer rejected
        bad = httpx.get(
            f"{base}/health",
            headers={"Authorization": "Bearer wrong-token-value-12345678901234567890"},
            timeout=10,
        )
        if bad.status_code != 401:
            raise AssertionError_(f"wrong bearer accepted: HTTP {bad.status_code}")
        # missing bearer rejected
        missing = httpx.get(f"{base}/health", timeout=10)
        if missing.status_code != 401:
            raise AssertionError_(f"missing bearer accepted: HTTP {missing.status_code}")
        manager.stop()

    def approval_and_dry_run(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        body = manager.health()
        if body.get("require_approval") is not True:
            raise AssertionError_(f"require_approval not true: {body}")
        if body.get("dry_run") is not True:
            raise AssertionError_(f"dry_run not true at acceptance start: {body}")
        manager.stop()

    def tiger_state_no_leak(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        body = manager.health()
        if "tiger_configured" not in body:
            raise AssertionError_("tiger_configured not reported")
        secret_values = [
            v
            for v in (
                manager.env.get("TIGEROPEN_TIGER_ID"),
                manager.env.get("TIGEROPEN_ACCOUNT"),
                manager.env.get("TIGEROPEN_PRIVATE_KEY_PATH"),
            )
            if v
        ]
        text = str(body)
        for value in secret_values:
            if value in text:
                raise AssertionError_(f"credential value leaked in health: {value[:6]}...")
        manager.stop()

    def no_secret_leak_in_output(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        output = manager.output_since()
        manager.stop()
        secret_values = [
            v
            for v in (
                manager.env.get("TRADEHUB_API_TOKEN"),
                manager.env.get("TIGEROPEN_TIGER_ID"),
                manager.env.get("TIGEROPEN_ACCOUNT"),
            )
            if v and len(v) >= 4
        ]
        for value in secret_values:
            if value in output:
                raise AssertionError_("configured secret appeared in service output")

    return PackDefinition(
        pack_id="FA-01",
        environment="local",
        depends_on=["FA-00"],
        assertions=[
            AssertionSpec("repo.commit_recorded", commit_recorded),
            AssertionSpec("install.import_succeeds", import_succeeds),
            AssertionSpec("gates.pytest_ruff_green", test_gates_green, timeout_seconds=300),
            AssertionSpec("service.starts_healthy", service_starts),
            AssertionSpec("bind.loopback_only_hard_fail", loopback_only),
            AssertionSpec("auth.valid_bearer_succeeds", auth_enforced),
            AssertionSpec("approval.require_approval_true", approval_and_dry_run),
            AssertionSpec("tiger.state_no_secret_leak", tiger_state_no_leak),
            AssertionSpec("output.no_secret_leak", no_secret_leak_in_output),
        ],
        safe_summary=(
            "Local runtime safety preflight passed: loopback bind, auth, "
            "approval, dry-run, no secret leakage."
        ),
    )

"""Local TradeHub service lifecycle management for acceptance packs.

The runner — never the agent — owns starting/stopping the deployed
service, waiting for health, and verifying the bind address. This keeps
FA-01/FA-03/FA-04/FA-05 deterministic across runs and restarts.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionBlocked,
    AssertionError_,
    RunContext,
)

VENV_BIN = REPO_ROOT / ".venv" / "bin"


def load_env_file(path: Path) -> dict[str, str]:
    """Load a simple KEY=VALUE .env file (values returned unexpanded)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


class ServiceManager:
    """Owns the tradehub subprocess lifecycle for one acceptance run."""

    def __init__(self, ctx: RunContext, env_overrides: dict[str, str] | None = None):
        self.ctx = ctx
        self.env = os.environ.copy()
        env_path = REPO_ROOT / ".env"
        for key, value in load_env_file(env_path).items():
            self.env.setdefault(key, value)
        if env_overrides:
            self.env.update(env_overrides)
        self.port = int(self.env.get("TRADEHUB_PORT", "8787"))
        self.host = self.env.get("TRADEHUB_BIND_HOST", "127.0.0.1")
        self._process: subprocess.Popen[str] | None = None
        self._log_file = None
        self.log_path: Path | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self, wait_seconds: int = 30) -> None:
        if self.is_listening():
            raise AssertionBlocked(
                f"port {self.port} already has a listener; refusing to start a second instance"
            )
        log_path = REPO_ROOT / "data" / "acceptance" / f"service-{self.ctx.run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self._log_file = open(log_path, "w", encoding="utf-8")
        self._process = subprocess.Popen(
            [str(VENV_BIN / "tradehub")],
            cwd=REPO_ROOT,
            env=self.env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            code = self._process.poll()
            if code is not None:
                raise AssertionError_(
                    f"tradehub exited early with code {code}: {self.output_since()[-2000:]}"
                )
            if self.is_listening() and self.health_ok():
                return
            time.sleep(0.5)
        raise AssertionError_(f"tradehub did not become healthy within {wait_seconds}s")

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None
        if getattr(self, "_log_file", None) is not None:
            self._log_file.close()
            self._log_file = None

    def restart(self, wait_seconds: int = 30) -> None:
        self.stop()
        self.start(wait_seconds=wait_seconds)

    # -- probes -----------------------------------------------------------

    def is_listening(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=1.0):
                return True
        except OSError:
            return False

    def health(self) -> dict[str, Any]:
        token = self.env.get("TRADEHUB_API_TOKEN", "")
        response = httpx.get(
            f"http://{self.host}:{self.port}/health",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def health_ok(self) -> bool:
        try:
            body = self.health()
            return bool(body.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    def bind_addresses(self) -> list[str]:
        """Return listening local addresses for the configured port.

        Uses `ss -tlnp` and parses the local address column. Non-loopback
        entries (0.0.0.0, ::, or a LAN IP) are a hard FAIL for FA-01.
        """
        import re

        try:
            result = subprocess.run(
                ["ss", "-tlnp"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise AssertionError_(f"cannot inspect listeners: {exc}") from exc
        addresses: list[str] = []
        for line in result.stdout.splitlines():
            if re.search(rf":{self.port}\b", line):
                parts = line.split()
                if len(parts) >= 4:
                    addresses.append(parts[3])
        return addresses

    def output_since(self) -> str:
        return self._drain_output()

    def _drain_output(self) -> str:
        """Read whatever the service has written to its log file.

        Non-blocking by design: the child writes to a file, so reading
        the file can never hang waiting for the child to exit.
        """
        if getattr(self, "_log_file", None) is None or self.log_path is None:
            return ""
        try:
            self._log_file.flush()
            return self.log_path.read_text(errors="replace") if self.log_path.exists() else ""
        except Exception:  # noqa: BLE001
            return ""


# --- shared service lifecycle -------------------------------------------


def get_service(ctx: RunContext, env_overrides: dict[str, str] | None = None) -> ServiceManager:
    """Return a running TradeHub service for this acceptance run.

    One manager is stashed per run context and reused across assertions
    and MCP calls. If a healthy listener already exists on the configured
    port (e.g. a leftover from a previous local pack), it is reused;
    otherwise a fresh instance is started. When the requested env
    overrides differ from those last used, the service is restarted with
    the new overrides (e.g. FA-04's short-TTL instance).
    """
    manager = getattr(ctx, "_acceptance_service", None)
    used_overrides = getattr(ctx, "_acceptance_service_overrides", None)
    if manager is None:
        manager = ServiceManager(ctx, env_overrides=env_overrides)
        ctx._acceptance_service = manager
        ctx._acceptance_service_overrides = env_overrides
        manager.start()
    elif used_overrides != env_overrides:
        manager.stop()
        manager = ServiceManager(ctx, env_overrides=env_overrides)
        ctx._acceptance_service = manager
        ctx._acceptance_service_overrides = env_overrides
        manager.start()
    elif not manager.is_listening():
        manager.start()
    return manager


def stop_service(ctx: RunContext) -> None:
    manager = getattr(ctx, "_acceptance_service", None)
    if manager is not None:
        manager.stop()


def assert_loopback_only(manager: ServiceManager) -> None:
    addresses = manager.bind_addresses()
    if not addresses:
        raise AssertionError_("no listener found on the configured port")
    for address in addresses:
        host = address.rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError_(f"non-loopback bind detected: {address} (must be loopback-only)")


class TigerAccountProof:
    """Read-only Tiger account profile proof (used by FA-05).

    `get_managed_accounts` returns profiles with `account_type`; only a
    broker-reported value of PAPER satisfies the gate. sandbox_debug,
    account-number shape, filenames, and prose are never accepted.
    """

    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        self._profiles: list[Any] | None = None

    def fetch(self) -> list[Any]:
        from tigeropen.common.consts import Language
        from tigeropen.trade.trade_client import TradeClient

        settings = self.ctx.settings
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.tiger_open_config import TigerOpenClientConfig

        config = TigerOpenClientConfig(sandbox_debug=settings.tiger_sandbox)
        config.tiger_id = settings.tiger_id or ""
        config.account = settings.tiger_account or ""
        if settings.tiger_license:
            config.license = settings.tiger_license
        config.language = Language.en_US
        if settings.tiger_private_key_path:
            config.private_key = read_private_key(str(settings.tiger_private_key_path))
        client = TradeClient(config)
        self._profiles = client.get_managed_accounts(account=settings.tiger_account)
        if self._profiles is None:
            raise AssertionBlocked("Tiger get_managed_accounts returned no profiles")
        return self._profiles

    def prove_paper(self) -> str:
        return find_paper_account(self.fetch())


def find_paper_account(profiles: list[Any]) -> str:
    """Return the account id of the broker-reported PAPER profile.

    Deterministic and unit-testable in isolation: only an exact
    broker-reported `account_type == PAPER` satisfies the gate. LIVE,
    unknown, missing, or None account_type never passes.
    """
    for profile in profiles:
        account = getattr(profile, "account", None)
        account_type = str(getattr(profile, "account_type", "") or "").upper()
        if account_type == "PAPER":
            return str(account)
    kinds = [f"{getattr(p, 'account', '?')}:{getattr(p, 'account_type', None)}" for p in profiles]
    raise AssertionBlocked(
        f"no broker-reported PAPER account found in profile list ({', '.join(kinds)})"
    )

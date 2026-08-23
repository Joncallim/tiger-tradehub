"""Deterministic acceptance runner: `tradehub-acceptance run <PACK> --json`.

The runner owns everything about result classification:
- pack lookup (unknown packs fail closed);
- environment validation (offline/local/paper);
- bounded retries and timeouts for explicitly transient operations;
- assertion evaluation (deterministic code, never an LLM verdict);
- secret sanitisation of every output field;
- run ID and commit SHA capture;
- versioned JSON result generation.

The agent (DeepSeek V4 Flash) only dispatches packs by ID and reports
the structured result. It cannot pass per-run overrides: `run_pack`
accepts nothing but a pack ID, so a BLOCKED case can never be turned
into a write-capable case by changing arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradehub.acceptance.sanitize import Sanitizer, build_sanitizer
from tradehub.acceptance.schema import (
    AssertionResult,
    RunResult,
    Status,
    aggregate_status,
)
from tradehub.config import Settings, get_settings

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = REPO_ROOT / "data" / "acceptance"

# Runner-owned policy constants. These are fixed; the agent does not
# decide retry counts or timeouts per run.
DEFAULT_ASSERTION_TIMEOUT_SECONDS = 60
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 1.0


class AssertionError_(Exception):
    """Deterministic assertion failure -> pack FAIL."""


class AssertionBlocked(Exception):
    """Prerequisite/safety condition not satisfied -> pack BLOCKED."""


class AssertionEscalate(Exception):
    """Runner cannot deterministically classify -> pack ESCALATE."""


class AssertionSpec:
    def __init__(
        self,
        assertion_id: str,
        fn: Callable[[RunContext], Any],
        timeout_seconds: int = DEFAULT_ASSERTION_TIMEOUT_SECONDS,
        transient: bool = False,
    ):
        self.id = assertion_id
        self.fn = fn
        self.timeout_seconds = timeout_seconds
        self.transient = transient


class PackDefinition:
    def __init__(
        self,
        pack_id: str,
        environment: str,
        depends_on: list[str],
        assertions: list[AssertionSpec],
        safe_summary: str,
    ):
        self.pack_id = pack_id
        self.environment = environment
        self.depends_on = depends_on
        self.assertions = assertions
        self.safe_summary = safe_summary


class RunContext:
    """Per-run services: settings, sanitizer, retries, artifact helpers."""

    def __init__(self, settings: Settings, sanitizer: Sanitizer, run_id: str):
        self.settings = settings
        self.sanitizer = sanitizer
        self.run_id = run_id
        self.artifacts: list[str] = []
        self._extra_secrets: list[str] = []

    def register_secret(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self.sanitizer.register(value)
            self._extra_secrets.append(value)

    def retry(self, fn: Callable[[], Any], attempts: int = RETRY_ATTEMPTS) -> Any:
        """Bounded retry for explicitly transient operations."""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn()
            except AssertionError_:
                raise
            except Exception as exc:  # noqa: BLE001 - transient boundary
                last = exc
                if attempt < attempts - 1:
                    time.sleep(RETRY_BASE_DELAY_SECONDS * (attempt + 1))
        assert last is not None
        raise AssertionError_(f"transient operation failed after {attempts} attempts: {last}")

    def write_artifact(self, name: str, payload: dict[str, Any]) -> str:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_DIR / f"{self.run_id}-{name}.json"
        path.write_text(json.dumps(self.sanitizer.sanitize_value(payload), indent=2))
        self.artifacts.append(str(path))
        return str(path)


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def capture_commit_sha() -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={REPO_ROOT}",
                "rev-parse",
                "HEAD",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    raise AssertionEscalate("cannot capture repository commit SHA")


def make_run_id(pack_id: str) -> str:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    normalized = pack_id.lower().replace("-", "")
    return f"{normalized}-{stamp}-{secrets.token_hex(4)}"


def evaluate_assertion(spec: AssertionSpec, ctx: RunContext) -> AssertionResult:
    """Run one assertion with a runner-owned timeout.

    Classification is deterministic:
    - AssertionError_  -> FAIL
    - AssertionBlocked -> BLOCKED
    - any other exception -> ESCALATE (cannot classify / needs diagnosis)
    - timeout          -> ESCALATE (cannot classify a hung boundary)
    """
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        if spec.transient:
            spec.fn(ctx)  # fn must use ctx.retry itself for bounded retries
        else:
            future = pool.submit(spec.fn, ctx)
            try:
                future.result(timeout=spec.timeout_seconds)
            except FutureTimeout:
                raise AssertionEscalate(
                    f"assertion timed out after {spec.timeout_seconds}s"
                ) from None
        status, detail = Status.PASS, ""
    except AssertionError_ as exc:
        status, detail = Status.FAIL, str(exc)
    except AssertionBlocked as exc:
        status, detail = Status.BLOCKED, str(exc)
    except AssertionEscalate as exc:
        status, detail = Status.ESCALATE, str(exc)
    except Exception as exc:  # noqa: BLE001 - unexpected boundary
        status, detail = Status.ESCALATE, f"unexpected {type(exc).__name__}: {exc}"
    finally:
        # Never join a hung worker: a single stuck assertion must not hang
        # the whole pack. The timeout is classified as ESCALATE above and
        # the run proceeds; the leaked worker thread is abandoned.
        pool.shutdown(wait=False, cancel_futures=True)
    elapsed = time.monotonic() - started
    detail = ctx.sanitizer.sanitize_text(detail) if detail else ""
    if status == Status.PASS:
        return AssertionResult(id=spec.id, status=status, detail=f"ok in {elapsed:.2f}s")
    return AssertionResult(id=spec.id, status=status, detail=detail)


def run_pack(pack_id: str, settings: Settings | None = None) -> RunResult:
    """Execute one pack by ID and return a sanitized, versioned result.

    Only a pack ID is accepted. No overrides exist, so blocked cases
    cannot be rescued by argument changes (fail-closed by construction).
    """
    from tradehub.acceptance.packs import PACKS

    started_at = utc_now()
    run_id = make_run_id(pack_id)
    sanitizer = build_sanitizer(settings)
    if settings is None:
        try:
            settings = get_settings()
        except Exception:  # noqa: BLE001
            sanitizer = build_sanitizer()

    if pack_id not in PACKS:
        return RunResult(
            pack_id=pack_id,
            run_id=run_id,
            environment="offline",
            status=Status.FAIL,
            commit_sha="unknown",
            started_at=started_at,
            finished_at=utc_now(),
            assertions=[
                AssertionResult(
                    id="pack.lookup",
                    status=Status.FAIL,
                    detail=f"unknown pack: {pack_id} (fail closed)",
                )
            ],
            safe_summary="Fail closed: unknown pack.",
        )

    pack = PACKS[pack_id]
    ctx = RunContext(settings, sanitizer, run_id)
    results = [evaluate_assertion(spec, ctx) for spec in pack.assertions]
    status = aggregate_status(results)

    try:
        commit_sha = capture_commit_sha()
    except AssertionEscalate:
        commit_sha = "unavailable"

    result = RunResult(
        pack_id=pack_id,
        run_id=run_id,
        environment=pack.environment,
        status=status,
        commit_sha=commit_sha,
        started_at=started_at,
        finished_at=utc_now(),
        assertions=results,
        artifacts=ctx.artifacts,
        safe_summary=pack.safe_summary,
        escalation_reason=next((a.detail for a in results if a.status == Status.ESCALATE), None),
    )
    # Persist sanitized run state for downstream lineage gates (FA-05).
    from tradehub.acceptance.state import record_run

    record_run(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradehub-acceptance")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run one acceptance pack")
    run_parser.add_argument("pack", help="Pack ID, e.g. FA-00")
    run_parser.add_argument("--json", action="store_true", help="Emit versioned JSON result")
    run_parser.add_argument("--list", action="store_true", help="List available packs")
    run_parser.add_argument("--unsafe-flag", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error(f"unknown command: {args.command}")

    if args.list:
        from tradehub.acceptance.packs import PACKS

        for pack_id in sorted(PACKS):
            pack = PACKS[pack_id]
            print(f"{pack_id}\t{pack.environment}\t{pack.safe_summary}")
        return 0

    # Any unsupported flag is rejected fail-closed (no argument overrides).
    if getattr(args, "unsafe_flag", False):
        result = RunResult(
            pack_id=args.pack,
            run_id=make_run_id(args.pack),
            environment="offline",
            status=Status.FAIL,
            commit_sha="unknown",
            started_at=utc_now(),
            finished_at=utc_now(),
            assertions=[
                AssertionResult(
                    id="cli.arguments",
                    status=Status.FAIL,
                    detail="unsupported flag rejected (fail closed)",
                )
            ],
            safe_summary="Fail closed: unsupported flags are rejected.",
        )
        print(result.model_dump_json(indent=2))
        return 1

    result = run_pack(args.pack)
    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(f"{result.pack_id} {result.status} {result.run_id}")
        for assertion in result.assertions:
            print(f"  [{assertion.status}] {assertion.id}: {assertion.detail}")
        if result.escalation_reason:
            print(f"  escalation: {result.escalation_reason}")

    return {Status.PASS: 0, Status.FAIL: 1, Status.BLOCKED: 2, Status.ESCALATE: 3}[result.status]


if __name__ == "__main__":
    sys.exit(main())

"""FA-00 — Acceptance runner + DeepSeek V4 Flash qualification.

Environment: offline.

This pack qualifies the *tester and the runner contract*, not the
trading system. It contains a deterministic fixture suite with known
PASS / FAIL / BLOCKED / ESCALATE cases, including:

- transient/retryable situations handled by the runner (not the agent);
- malformed result payloads that must fail closed;
- unsafe environments (paper/live mismatch) that must BLOCK;
- attempts to turn a blocked case into a write-capable case by changing
  arguments, which must fail closed;
- secret sanitisation of result output.

The agent is NOT allowed to change runner arguments, criteria, policy,
or source to rescue a blocked/failed fixture. The runner evaluates each
fixture deterministically; the agent only reports the structured result.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from tradehub.acceptance.runner import (
    AssertionBlocked,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
    evaluate_assertion,
)
from tradehub.acceptance.sanitize import Sanitizer
from tradehub.acceptance.schema import Status

# ---------------------------------------------------------------------------
# Deterministic fixture universe: each fixture is a scenario with fixed
# inputs and a known expected terminal state.
# ---------------------------------------------------------------------------


class Fixture:
    def __init__(
        self,
        fixture_id: str,
        expected: Status,
        safety_critical: bool,
        run: Callable[[RunContext], Status],
    ):
        self.id = fixture_id
        self.expected = expected
        self.safety_critical = safety_critical
        self.run = run


def _fake_settings() -> Any:
    class _Fake:
        def __init__(self) -> None:
            self.dry_run = True
            self.tiger_configured = False
            self.require_approval = True

        def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "dry_run": self.dry_run,
                "tiger_configured": self.tiger_configured,
                "require_approval": self.require_approval,
            }

    return _Fake()


def _ctx(run_id: str = "fixture-run") -> RunContext:
    return RunContext(_fake_settings(), Sanitizer(), run_id)


# --- PASS fixture: everything green --------------------------------------
def _fixture_pass(ctx: RunContext) -> Status:
    outcomes: list[Status] = []
    for label, fn in [
        ("green.health", lambda _: None),
        ("green.auth", lambda _: None),
        ("green.dry_run", lambda _: None),
    ]:
        outcomes.append(evaluate_assertion(AssertionSpec(label, fn), ctx).status)
    assert outcomes == [Status.PASS, Status.PASS, Status.PASS], outcomes
    return Status.PASS


# --- FAIL fixture: a deterministic assertion is false --------------------
def _fixture_fail(ctx: RunContext) -> Status:
    def bad_assertion(_: RunContext) -> None:
        raise AssertionError_("dry_run unexpectedly false")

    outcome = evaluate_assertion(AssertionSpec("fail.dry_run", bad_assertion), ctx)
    assert outcome.status == Status.FAIL, outcome.status
    return outcome.status


# --- BLOCKED fixture: precondition not satisfied -------------------------
def _fixture_blocked(ctx: RunContext) -> Status:
    def blocked_assertion(_: RunContext) -> None:
        raise AssertionBlocked("Tiger credentials are not configured")

    outcome = evaluate_assertion(AssertionSpec("blocked.tiger_creds", blocked_assertion), ctx)
    assert outcome.status == Status.BLOCKED, outcome.status
    return outcome.status


# --- ESCALATE fixture: cannot be deterministically classified ------------
def _fixture_escalate(ctx: RunContext) -> Status:
    def unexpected(_: RunContext) -> None:
        raise RuntimeError("unexpected internal failure")

    outcome = evaluate_assertion(AssertionSpec("escalate.internal", unexpected), ctx)
    assert outcome.status == Status.ESCALATE, outcome.status
    return outcome.status


# --- Transient/retryable: runner retries, agent does not improvise -------
class _Flaky:
    def __init__(self, fail_times: int):
        self.left = fail_times

    def __call__(self) -> None:
        if self.left > 0:
            self.left -= 1
            raise ConnectionError("transient upstream blip")
        return None


def _fixture_transient(ctx: RunContext) -> Status:
    flaky = _Flaky(fail_times=2)
    # Bounded retry owned by the runner: 3 attempts, succeeds on the third.
    ctx.retry(flaky, attempts=3)
    return Status.PASS


# --- Malformed output: schema validation fails closed --------------------
def _fixture_malformed(ctx: RunContext) -> Status:
    # Simulate an AssertionSpec that returns a payload the schema rejects:
    # we emulate by producing a result dict missing required fields and
    # verifying the schema rejects it (fail closed, never a silent pass).
    from tradehub.acceptance.schema import RunResult

    try:
        RunResult.model_validate({"pack_id": "FA-00"})  # missing required fields
    except Exception:  # noqa: BLE001 - expected validation failure
        return Status.PASS
    raise AssertionError_("malformed result payload unexpectedly validated")


# --- Unsafe environment: paper/live mismatch must BLOCK ------------------
LIVE_ACCOUNT_PROFILE = {"account_type": "LIVE", "account": "U12345678"}
PAPER_ACCOUNT_PROFILE = {"account_type": "PAPER", "account": "TEST-PAPER-ACCOUNT-PLACEHOLDER"}


def _prove_paper(profile: dict[str, str]) -> None:
    if profile.get("account_type") != "PAPER":
        raise AssertionBlocked(
            f"broker-reported account_type={profile.get('account_type')!r} is not PAPER"
        )


def _fixture_paper_live_mismatch(ctx: RunContext) -> Status:
    # sandbox_debug=true but broker reports LIVE: must BLOCK, never PASS,
    # and never pretend a write is safe.
    ctx.register_secret("U12345678")
    try:
        _prove_paper(LIVE_ACCOUNT_PROFILE)
    except AssertionBlocked:
        return Status.BLOCKED
    raise AssertionError_("live account was accepted as paper (unsafe)")


def _fixture_paper_paper_match(ctx: RunContext) -> Status:
    try:
        _prove_paper(PAPER_ACCOUNT_PROFILE)
    except AssertionBlocked:
        return Status.BLOCKED
    return Status.PASS


# --- Blocked case cannot become write-capable by changing args -----------
def _fixture_no_argument_escape(ctx: RunContext) -> Status:
    profile = dict(PAPER_ACCOUNT_PROFILE)
    # An agent tries to force the case through by mutating the profile:
    # the runner's proof function must still refuse anything but PAPER.
    attempt = dict(profile)
    attempt["account_type"] = "LIVE"
    attempt["account"] = "TEST-PAPER-ACCOUNT-PLACEHOLDER"
    try:
        _prove_paper(attempt)
        return Status.FAIL
    except AssertionBlocked:
        return Status.PASS
    try:
        _prove_paper({**profile, "forged_proof": True})
        return Status.FAIL
    except AssertionBlocked:
        return Status.PASS


# --- Secret sanitisation -------------------------------------------------
def _fixture_sanitize(ctx: RunContext) -> Status:
    ctx.register_secret("super-secret-api-token-value")
    ctx.register_secret("TEST-TIGER-ID-PLACEHOLDER")
    ctx.register_secret("TEST-PAPER-ACCOUNT-PLACEHOLDER")
    raw = {
        "token": "super-secret-api-token-value",
        "tiger_id": "TEST-TIGER-ID-PLACEHOLDER",
        "account": "TEST-PAPER-ACCOUNT-PLACEHOLDER",
        "confirmation_token": "AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "nested": {"a": ["super-secret-api-token-value"]},
        "safe": "AAPL order preview ok",
    }
    cleaned = ctx.sanitizer.sanitize_value(raw)
    text = json.dumps(cleaned)
    for secret in [
        "super-secret-api-token-value",
        "TEST-TIGER-ID-PLACEHOLDER",
        "TEST-PAPER-ACCOUNT-PLACEHOLDER",
        "AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    ]:
        assert secret not in text, f"secret leaked into result: {secret}"
    assert "AAPL order preview ok" in text
    return Status.PASS


# --- Transient exhaustion: bounded retries give up deterministically -----
def _fixture_retry_exhausted(ctx: RunContext) -> Status:
    flaky = _Flaky(fail_times=99)
    try:
        ctx.retry(flaky, attempts=2)
        return Status.FAIL  # should never succeed
    except AssertionError_:
        return Status.PASS


FIXTURES: list[Fixture] = [
    Fixture("pass_all_green", Status.PASS, False, _fixture_pass),
    Fixture("fail_deterministic", Status.FAIL, False, _fixture_fail),
    Fixture("blocked_precondition", Status.BLOCKED, False, _fixture_blocked),
    Fixture("escalate_unclassifiable", Status.ESCALATE, True, _fixture_escalate),
    Fixture("transient_retry_recovers", Status.PASS, True, _fixture_transient),
    Fixture("malformed_fails_closed", Status.PASS, True, _fixture_malformed),
    Fixture("paper_live_mismatch", Status.BLOCKED, True, _fixture_paper_live_mismatch),
    Fixture("paper_paper_match", Status.PASS, True, _fixture_paper_paper_match),
    Fixture("no_argument_escape", Status.PASS, True, _fixture_no_argument_escape),
    Fixture("secret_sanitisation", Status.PASS, True, _fixture_sanitize),
    Fixture("retry_exhaustion_blocked", Status.PASS, True, _fixture_retry_exhausted),
]


def build_fa00_pack() -> PackDefinition:
    def qualification(ctx: RunContext) -> None:
        mismatches: list[str] = []
        for fixture in FIXTURES:
            ctx.register_secret("TEST-TIGER-ID-PLACEHOLDER")
            ctx.register_secret("TEST-PAPER-ACCOUNT-PLACEHOLDER")
            actual = fixture.run(ctx)
            if actual != fixture.expected:
                mismatches.append(
                    f"{fixture.id}: expected {fixture.expected.value}, got {actual.value}"
                )
        if mismatches:
            raise AssertionError_(
                "qualification fixture mismatch: " + "; ".join(sorted(mismatches))
            )

    def safety_critical_hundred_percent(ctx: RunContext) -> None:
        for fixture in FIXTURES:
            if not fixture.safety_critical:
                continue
            actual = fixture.run(ctx)
            if actual != fixture.expected:
                raise AssertionError_(
                    f"safety-critical fixture {fixture.id}: expected "
                    f"{fixture.expected.value}, got {actual.value}"
                )

    def unknown_pack_fails_closed(ctx: RunContext) -> None:
        from tradehub.acceptance.packs import PACKS

        assert "FA-99-DOES-NOT-EXIST" not in PACKS
        from tradehub.acceptance.runner import run_pack

        result = run_pack("FA-99-DOES-NOT-EXIST")
        if result.status != Status.FAIL:
            raise AssertionError_(f"unknown pack did not fail closed: {result.status.value}")
        if result.assertions[0].id != "pack.lookup":
            raise AssertionError_("unknown pack did not produce pack.lookup failure")

    def schema_is_stable(ctx: RunContext) -> None:
        from tradehub.acceptance.schema import RunResult

        result = RunResult(
            pack_id="FA-00",
            run_id="schema-test-run",
            environment="offline",
            status=Status.PASS,
            commit_sha="deadbeef",
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        payload = result.model_dump()
        assert payload["schema_version"] == 1
        assert payload["status"] == "PASS"
        assert set(payload) >= {
            "schema_version",
            "pack_id",
            "run_id",
            "environment",
            "status",
            "commit_sha",
        }

    def runner_owns_retries(ctx: RunContext) -> None:
        # A transient failure followed by success must pass via the runner's
        # bounded retry; the fixture does NOT call retry itself here.
        flaky = _Flaky(fail_times=1)
        ctx.retry(flaky, attempts=3)

    return PackDefinition(
        pack_id="FA-00",
        environment="offline",
        depends_on=[],
        assertions=[
            AssertionSpec("qualification.all_fixtures_match", qualification),
            AssertionSpec("qualification.safety_critical_100pct", safety_critical_hundred_percent),
            AssertionSpec("fail_closed.unknown_pack", unknown_pack_fails_closed),
            AssertionSpec("schema.versioned_stable", schema_is_stable),
            AssertionSpec("retry.runner_owned_bounded", runner_owns_retries),
        ],
        safe_summary="Acceptance runner contract and DeepSeek qualification fixtures all PASS.",
    )

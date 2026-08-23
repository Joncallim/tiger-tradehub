"""Offline acceptance-runner tests (safe for GitHub-hosted CI).

These tests exercise the runner contract and the FA-00 qualification
fixtures deterministically, without Tiger credentials or a running
TradeHub service.
"""

import contextlib
import io
import json
from pathlib import Path

from tradehub.acceptance.packs.fa00 import FIXTURES
from tradehub.acceptance.runner import (
    AssertionBlocked,
    AssertionError_,
    AssertionSpec,
    RunContext,
    evaluate_assertion,
    main,
    run_pack,
)
from tradehub.acceptance.sanitize import Sanitizer
from tradehub.acceptance.schema import RunResult, Status, aggregate_status


def _ctx() -> RunContext:
    return RunContext(
        type("S", (), {"model_dump": lambda self: {}})(),
        Sanitizer(),
        "test-run-id",
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_result_schema_is_versioned_and_stable():
    result = RunResult(
        pack_id="FA-00",
        run_id="fa00-20260823T000000Z-abc123",
        environment="offline",
        status=Status.PASS,
        commit_sha="deadbeef",
        started_at="2026-08-23T00:00:00Z",
        finished_at="2026-08-23T00:00:01Z",
    )
    payload = result.model_dump()
    assert payload["schema_version"] == 1
    assert set(payload) >= {
        "schema_version",
        "pack_id",
        "run_id",
        "environment",
        "status",
        "commit_sha",
        "assertions",
        "artifacts",
        "safe_summary",
    }


def test_aggregate_status_ordering_is_fixed():
    def a(status):
        from tradehub.acceptance.schema import AssertionResult

        return AssertionResult(id="x", status=status)

    assert aggregate_status([a(Status.PASS)]) == Status.PASS
    assert aggregate_status([a(Status.PASS), a(Status.BLOCKED)]) == Status.BLOCKED
    assert aggregate_status([a(Status.BLOCKED), a(Status.FAIL)]) == Status.FAIL
    assert aggregate_status([a(Status.FAIL), a(Status.ESCALATE)]) == Status.ESCALATE


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


def test_sanitizer_redacts_secrets():
    sanitizer = Sanitizer()
    sanitizer.register("tiger-id-123")
    sanitizer.register("21155143479478282")
    sanitizer.register("sekrit-token-value-42")
    cleaned = sanitizer.sanitize_value(
        {
            "tiger_id": "tiger-id-123",
            "account": "21155143479478282",
            "confirmation_token": "AbCdEfGhIjKlMnOpQrStUvWxYz012345",
            "nested": ["21155143479478282", "ok-value"],
            "private_key": "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----",
        }
    )
    text = json.dumps(cleaned)
    assert "tiger-id-123" not in text
    assert "21155143479478282" not in text
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in text
    assert "BEGIN PRIVATE KEY" not in text
    assert "ok-value" in text


def test_sanitizer_redacts_secret_in_plain_text():
    sanitizer = Sanitizer()
    sanitizer.register("super-secret-token")
    assert "super-secret-token" not in sanitizer.sanitize_text(
        "error with super-secret-token inside"
    )


# ---------------------------------------------------------------------------
# Assertion classification
# ---------------------------------------------------------------------------


def test_assertion_classification_is_deterministic():
    ctx = _ctx()

    def ok(_):
        return None

    def fail(_):
        raise AssertionError_("boom")

    def blocked(_):
        raise AssertionBlocked("no creds")

    def escalate(_):
        raise RuntimeError("unexpected")

    assert evaluate_assertion(AssertionSpec("ok", ok), ctx).status == Status.PASS
    assert evaluate_assertion(AssertionSpec("fail", fail), ctx).status == Status.FAIL
    assert evaluate_assertion(AssertionSpec("blocked", blocked), ctx).status == Status.BLOCKED
    assert evaluate_assertion(AssertionSpec("escalate", escalate), ctx).status == Status.ESCALATE


def test_unknown_pack_fails_closed():
    result = run_pack("FA-99-DOES-NOT-EXIST")
    assert result.status == Status.FAIL
    assert result.assertions[0].id == "pack.lookup"


def test_cli_rejects_unsupported_flags():

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["run", "FA-00", "--unsafe-flag"])
    assert code == 1
    payload = json.loads(buffer.getvalue())
    assert payload["status"] == "FAIL"
    assert payload["assertions"][0]["id"] == "cli.arguments"


# ---------------------------------------------------------------------------
# FA-00 qualification fixtures
# ---------------------------------------------------------------------------


def test_fixture_set_contains_all_required_classes():
    by_expected = {}
    for fixture in FIXTURES:
        by_expected.setdefault(fixture.expected, []).append(fixture.id)
    for expected in (Status.PASS, Status.FAIL, Status.BLOCKED, Status.ESCALATE):
        assert expected in by_expected, f"missing fixture for {expected}"
    assert any(f.safety_critical for f in FIXTURES)


def test_fa00_pack_passes_fully():
    result = run_pack("FA-00")
    assert result.status == Status.PASS, result.model_dump_json(indent=2)
    assert result.environment == "offline"


def test_fa00_run_is_versioned_json():

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["run", "FA-00", "--json"])
    assert code == 0
    payload = json.loads(buffer.getvalue())
    assert payload["schema_version"] == 1
    assert payload["pack_id"] == "FA-00"
    assert payload["status"] == "PASS"
    assert payload["run_id"].startswith("fa00-")


def test_fa00_artifacts_contain_no_secrets():
    result = run_pack("FA-00")
    assert result.status == Status.PASS
    for path in result.artifacts:
        text = Path(path).read_text()
        for secret in ("21155143479478282", "20161327", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"):
            assert secret not in text, f"secret leaked into artifact {path}"

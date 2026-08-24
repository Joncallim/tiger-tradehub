"""Offline acceptance-runner tests (safe for GitHub-hosted CI).

These tests exercise the runner contract and the FA-00 qualification
fixtures deterministically, without Tiger credentials or a running
TradeHub service.
"""

import contextlib
import io
import json
from pathlib import Path

import pytest

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


def test_run_sanitizer_includes_settings_secrets():
    """CLI-run sanitizer must include real settings values (regression:
    the sanitizer used to be built empty before settings were resolved,
    letting configured credentials leak into reports)."""
    from tradehub.config import Settings

    settings = Settings(
        TRADEHUB_API_TOKEN="test-token-with-enough-length-1234",
        TIGEROPEN_TIGER_ID="20161327",
        TIGEROPEN_ACCOUNT="21155143479478282",
    )
    result = run_pack("FA-00", settings=settings)
    text = result.model_dump_json()
    assert "21155143479478282" not in text
    assert "20161327" not in text


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


def test_hung_assertion_escalates_without_hanging_pack():
    """A stuck assertion must classify ESCALATE and never hang the run."""
    import time

    from tradehub.acceptance.runner import PackDefinition, run_pack

    def _hang(_):
        time.sleep(60)  # longer than the assertion timeout

    pack = PackDefinition(
        pack_id="FA-HANG-TEST",
        environment="offline",
        depends_on=[],
        assertions=[AssertionSpec("hang.", _hang, timeout_seconds=2)],
        safe_summary="test",
    )
    from tradehub.acceptance.packs import PACKS

    PACKS["FA-HANG-TEST"] = pack
    try:
        started = time.monotonic()
        result = run_pack("FA-HANG-TEST")
        elapsed = time.monotonic() - started
        assert elapsed < 15, f"pack hung for {elapsed:.1f}s"
        assert result.status == Status.ESCALATE
        assert result.assertions[0].detail.startswith("assertion timed out")
    finally:
        PACKS.pop("FA-HANG-TEST", None)


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


# ---------------------------------------------------------------------------
# FA-05 safety semantics (delayed-quote paper lifecycle)
# ---------------------------------------------------------------------------


class _Profile:
    def __init__(self, account, account_type):
        self.account = account
        self.account_type = account_type


def test_fa05_paper_gate_rejects_live_account():
    from tradehub.acceptance.service import find_paper_account

    with pytest.raises(AssertionBlocked):
        find_paper_account([_Profile("U12345678", "LIVE")])


def test_fa05_paper_gate_rejects_unknown_or_missing_type():
    from tradehub.acceptance.service import find_paper_account

    with pytest.raises(AssertionBlocked):
        find_paper_account([_Profile("21155143479478282", None)])
    with pytest.raises(AssertionBlocked):
        find_paper_account([_Profile("21155143479478282", "UNKNOWN")])
    with pytest.raises(AssertionBlocked):
        find_paper_account([])


def test_fa05_paper_gate_accepts_broker_paper_profile():
    from tradehub.acceptance.service import find_paper_account

    account = find_paper_account(
        [_Profile("U12345678", "LIVE"), _Profile("21155143479478282", "PAPER")]
    )
    assert account == "21155143479478282"


def test_fa05_limit_is_deterministically_derived():
    from tradehub.acceptance.packs.fa05 import (
        ACCEPTANCE_LIMIT_FRACTION,
        derive_acceptance_limit,
    )

    assert derive_acceptance_limit(100.00) == 50.0
    assert derive_acceptance_limit(180.00) == round(180.00 * ACCEPTANCE_LIMIT_FRACTION, 2)
    assert derive_acceptance_limit(1.37) == round(1.37 * ACCEPTANCE_LIMIT_FRACTION, 2)


def test_fa05_limit_rejects_bad_reference_and_caps():
    from tradehub.acceptance.packs.fa05 import derive_acceptance_limit

    with pytest.raises(AssertionBlocked):
        derive_acceptance_limit(0)
    with pytest.raises(AssertionBlocked):
        derive_acceptance_limit(-5)
    with pytest.raises(AssertionBlocked):
        derive_acceptance_limit(None)
    # qty 1, notional cap 100: a 250 reference at 0.5 => 125 notional -> blocked
    with pytest.raises(AssertionBlocked):
        derive_acceptance_limit(250.00, quantity=1, max_notional_usd=100.0)


def test_fa05_delayed_quote_record_is_labeled_delayed():
    from tradehub.acceptance.packs.fa05 import _delayed_quote_record

    ctx = _ctx()
    record = _delayed_quote_record(ctx, 309.35, 1787342400000)
    assert record["classification"] == "DELAYED"
    assert record["source"] == "tiger_delayed_quote"
    assert record["is_real_time"] is False
    assert record["symbol"] == "AAPL"
    assert "staleness" in record
    assert record["staleness"].startswith("delayed_")


def test_fa05_delayed_quote_record_sanitizes_account_values():
    from tradehub.acceptance.packs.fa05 import _delayed_quote_record

    ctx = _ctx()
    ctx.register_secret("21155143479478282")
    record = _delayed_quote_record(ctx, 309.35, None)
    cleaned = ctx.sanitizer.sanitize_value(record)
    assert "21155143479478282" not in json.dumps(cleaned)


def test_fa05_pack_defines_no_market_order_path():
    """The pack must never construct a MARKET order.

    Only the preview payload's explicit LIMIT construction is the write
    path; no `OrderType.MARKET` usage and no market_order builder call
    may exist in the pack.
    """
    import inspect

    from tradehub.acceptance.packs import fa05

    source = inspect.getsource(fa05)
    assert '"order_type": "LIMIT"' in source  # write path is LIMIT only
    assert "OrderType.MARKET" not in source
    assert "market_order(" not in source


def test_fa05_pack_requires_write_flag_and_lineage_gates():
    """Pack gate assertions exist and BLOCK without the flag/lineage."""
    from tradehub.acceptance.packs import PACKS

    pack = PACKS["FA-05"]
    gate_ids = {a.id for a in pack.assertions}
    assert "gate.acceptance_write_flag" in gate_ids
    assert "gate.upstream_lineage" in gate_ids
    assert "gate.broker_paper_proof" in gate_ids
    assert "gate.delayed_reference_limit" in gate_ids
    assert "lifecycle.place_read_cancel_reconcile" in gate_ids

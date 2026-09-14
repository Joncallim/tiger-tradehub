"""P1 runner regressions: fixture authority and trade-quantity semantics.

P1-1  A production envelope must NEVER be able to exempt itself from
      proposal-authority validation. Fixture permission is selected OUT OF BAND
      by an explicitly isolated harness (``run_autonomy(fixture_mode=True)``),
      is impossible to enable from envelope contents, and is not enabled by the
      deployed systemd unit.

P1-2  ``completion_quantity_microunits`` is the TARGET post-trade holding.
      ``max_quantity_microunits`` is the ORDER DELTA. The order size is always
      the delta, and the translated quantity/notional are independently asserted
      against the proposal bounds before any budget charge or preview.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.test_autonomy_paper import (  # noqa: F401
    NOW,
    FakeClient,
    _envelope,
    _run,
    _write_inbox,
)
from tests.test_autonomy_paper import ctx as _harness_ctx
from tradehub.autonomy.budgets import daily_usage
from tradehub_research.ops.decision_pipeline import (
    AUTHORITY_SCHEMA_VERSION,
    _publish_authority,
)
from tradehub_research.screens import canonical_json

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "tradehub-paper-autonomy.service"

SHARE = 1_000_000


@pytest.fixture()
def ctx(tmp_path):  # noqa: F811
    """The shared isolated acceptance-harness context.

    Built from the harness fixture's underlying function so this module owns a
    real, locally-registered fixture name.
    """
    return _harness_ctx.__wrapped__(tmp_path)


def _identity(envelope: dict) -> str:
    stable = {k: v for k, v in envelope.items() if k != "exported_at"}
    return hashlib.sha256(canonical_json(stable).encode()).hexdigest()


def _publish(ctx, envelope: dict, **overrides) -> dict:
    """Publish the authority record the exporter would have written."""
    proposal = envelope["proposal"]
    record = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "proposal_id": proposal["proposal_id"],
        "security_id": proposal["security_id"],
        "canonical_symbol": envelope["symbol"],
        "action": proposal["action"],
        "max_quantity_microunits": proposal["max_quantity_microunits"],
        "completion_quantity_microunits": proposal["completion_quantity_microunits"],
        "max_notional_microusd": proposal["max_notional_microusd"],
        "current_weight_ppm": proposal["current_weight_ppm"],
        "target_weight_ppm": proposal["target_weight_ppm"],
        "score_snapshot_id": proposal["score_snapshot_id"],
        "portfolio_snapshot_id": proposal["portfolio_snapshot_id"],
        "policy_version": proposal["policy_version"],
        "sizing_policy_version": proposal["sizing_policy_version"],
        "proposal_mode": "PAPER",
        "requires_human_approval": 0,
        "autonomy_eligible": True,
        "current_state": proposal["current_state"],
        "proposed_state": proposal["proposed_state"],
        "state_transition": f"{proposal['current_state']}->{proposal['proposed_state']}",
        "data_as_of": envelope["data_as_of"],
        "envelope_identity_hash": _identity(envelope),
        "created_at": "2026-08-31T12:00:00Z",
    }
    record.update(overrides)
    if "state_transition" not in overrides:
        record["state_transition"] = f"{record['current_state']}->{record['proposed_state']}"
    _publish_authority(ctx["authority_dir"], record)
    return record


def _preview_payloads(ctx) -> list[dict]:
    return [p for path, p in ctx["client"].calls if path == "/orders/preview"]


def _submit_calls(ctx) -> list[dict]:
    return [p for path, p in ctx["client"].calls if path == "/orders/submit"]


def _charged(ctx) -> int:
    return int(daily_usage(NOW.date().isoformat(), path=ctx["budget_db"])["count"])


# --------------------------------------------------------------------------
# P1-1  fixture authority cannot be self-granted from the inbox
# --------------------------------------------------------------------------
def test_production_refuses_forged_fixture_envelope_before_any_side_effect(ctx):  # noqa: F811
    """Production runner + fixture=true/tag + no authority => REFUSED, and
    zero budget charge, zero preview, zero submit."""
    _write_inbox(ctx, _envelope(fixture=True, fixture_tag="paper-acceptance-fixture-v1"))
    summary = _run(ctx, fixture_mode=False)

    assert summary["orders"] == 0
    assert summary["executions"] == []
    assert summary["refusals"], "a forged fixture envelope must be refused"
    assert any(
        "fixture authority" in str(entry.get("reason", "")) for entry in summary["refusals"]
    ), summary["refusals"]
    assert _charged(ctx) == 0, "must not charge budget"
    assert _preview_payloads(ctx) == [], "must not preview"
    assert _submit_calls(ctx) == [], "must not submit"


def test_production_refuses_nonfixture_envelope_without_authority(ctx):  # noqa: F811
    _write_inbox(ctx, _envelope(fixture=False))
    summary = _run(ctx, fixture_mode=False)
    assert summary["orders"] == 0
    assert any(
        "no persisted proposal authority" in str(entry.get("reason", ""))
        for entry in summary["refusals"]
    ), summary["refusals"]
    assert _charged(ctx) == 0
    assert _preview_payloads(ctx) == []


def test_forged_fixture_field_is_refused_even_with_a_real_authority_record(ctx):  # noqa: F811
    """A real authority record does not license fixture fields in the envelope."""
    envelope = _envelope(fixture=True)
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    summary = _run(ctx, fixture_mode=False)
    assert summary["orders"] == 0
    assert _preview_payloads(ctx) == []


def test_deployed_unit_does_not_enable_fixture_mode():
    text = UNIT.read_text(encoding="utf-8")
    assert "fixture" not in text.lower(), "the deployed systemd unit must never enable fixture mode"


def test_fixture_mode_defaults_to_false():
    import inspect

    from tradehub.autonomy.runner import run_autonomy

    default = inspect.signature(run_autonomy).parameters["fixture_mode"].default
    assert default is False


# --------------------------------------------------------------------------
# P1-2  order quantity is the DELTA, not the completion target
# --------------------------------------------------------------------------
def test_add_uses_delta_quantity_not_completion(ctx):  # noqa: F811
    """ADD: holding 10, buy 5, completion 15 => preview quantity 5, NOT 15."""
    envelope = _envelope(
        fixture=False,
        action="BUY",
        current_state="HOLD",
        proposed_state="ADD",
        quantity=5 * SHARE,
        completion=15 * SHARE,
        current_qty=10 * SHARE,
        sellable=10 * SHARE,
    )
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    summary = _run(ctx, fixture_mode=False)

    assert summary["orders"] == 1, summary["refusals"]
    payloads = _preview_payloads(ctx)
    assert len(payloads) == 1
    assert payloads[0]["quantity"] == 5, (
        f"expected the 5-share delta, got {payloads[0]['quantity']}"
    )
    assert payloads[0]["side"] == "BUY"


def test_trim_uses_delta_quantity_not_completion(ctx):  # noqa: F811
    """TRIM: holding 10, sell 4, completion 6 => preview quantity 4, NOT 6."""
    envelope = _envelope(
        fixture=False,
        action="SELL",
        current_state="HOLD",
        proposed_state="TRIM",
        quantity=4 * SHARE,
        completion=6 * SHARE,
        current_qty=10 * SHARE,
        sellable=10 * SHARE,
    )
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    summary = _run(ctx, fixture_mode=False)

    assert summary["orders"] == 1, summary["refusals"]
    payloads = _preview_payloads(ctx)
    assert len(payloads) == 1
    assert payloads[0]["quantity"] == 4
    assert payloads[0]["side"] == "SELL"


def test_enter_from_zero_is_also_the_delta(ctx):  # noqa: F811
    """ENTER from flat: buy 5, completion 5 => preview quantity 5."""
    envelope = _envelope(
        fixture=False,
        action="BUY",
        current_state="WATCH",
        proposed_state="ENTER",
        quantity=5 * SHARE,
        completion=5 * SHARE,
        current_qty=0,
    )
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    summary = _run(ctx, fixture_mode=False)
    assert summary["orders"] == 1, summary["refusals"]
    assert _preview_payloads(ctx)[0]["quantity"] == 5


def test_inconsistent_completion_is_refused_before_preview(ctx):  # noqa: F811
    """completion must be a consistent delta on the holding (10+5=15, not 12)."""
    envelope = _envelope(
        fixture=False,
        action="BUY",
        current_state="HOLD",
        proposed_state="ADD",
        quantity=5 * SHARE,
        completion=12 * SHARE,
        current_qty=10 * SHARE,
    )
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    summary = _run(ctx, fixture_mode=False)
    assert summary["orders"] == 0
    assert any("consistent" in str(entry.get("reason", "")) for entry in summary["refusals"]), (
        summary["refusals"]
    )
    assert _preview_payloads(ctx) == []
    assert _charged(ctx) == 0


def test_translated_notional_above_max_is_refused_before_charge_and_preview(ctx):  # noqa: F811
    """mark x quantity must not exceed the proposal's own max_notional."""

    class _CapturingClient(FakeClient):
        pass

    # 100 shares at $15 = $1500 = 1_500_000_000 microusd, but the proposal only
    # authorises 1_000_000_000 microusd ($1000).
    envelope = _envelope(
        fixture=False,
        action="BUY",
        quantity=100 * SHARE,
        completion=100 * SHARE,
        current_qty=0,
        mark=15_000_000,
        notional=1_000_000_000,
    )
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    summary = _run(ctx, fixture_mode=False)
    assert summary["orders"] == 0
    assert any(
        "translated notional" in str(entry.get("reason", "")) for entry in summary["refusals"]
    ), summary["refusals"]
    assert _preview_payloads(ctx) == []
    assert _charged(ctx) == 0


def test_duplicate_rerun_does_not_duplicate_the_order(ctx):  # noqa: F811
    """Same proposal twice => one execution; the second is a duplicate refusal."""
    envelope = _envelope(
        fixture=False,
        action="BUY",
        current_state="HOLD",
        proposed_state="ADD",
        quantity=5 * SHARE,
        completion=15 * SHARE,
        current_qty=10 * SHARE,
    )
    _publish(ctx, envelope)
    _write_inbox(ctx, envelope)
    first = _run(ctx, fixture_mode=False)
    assert first["orders"] == 1

    second = _run(ctx, fixture_mode=False)
    assert second["orders"] == 0
    assert len(_preview_payloads(ctx)) == 1, "no second preview for the same proposal"
    assert len(_submit_calls(ctx)) == 1
    assert _charged(ctx) == 1

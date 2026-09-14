"""Real producer -> real consumer proof for trade-quantity semantics.

An ENTER-from-flat test alone hides the quantity bug, so this module proves the
interface between the REAL sizing code that populates a proposal and the REAL
production runner that consumes it.

SCOPE — what is and is NOT proven here (kept explicit, do not overread):
  PROVEN:
    - the PRODUCER numbers are real: tradehub_research.portfolio.sizing
      size_buy/size_sell output, not hand-picked constants, and they are
      target-consistent (completion == current +/- delta; full exit -> 0).
    - the CONSUMER is real: the PRODUCTION runner (fixture_mode=False) forwards
      max_quantity_microunits (the delta) and never completion_quantity_microunits.
  NOT PROVEN HERE:
    - the persisted-proposal -> envelope field mapping. This module assembles the
      envelope per the documented contract rather than driving
      export_eligible_proposals, because the engine's state machine will not
      emit HOLD->ADD / HOLD->TRIM from a cold disposable DB. The real exporter IS
      exercised end-to-end for the ENTER case in
      tests/test_disposable_e2e_p66.py, and its proposal dict is the persisted
      row verbatim (only ticker/data_as_of/mark/current/sellable are re-added),
      so it cannot alias max_quantity onto completion. A real-chain ADD/TRIM
      export test is tracked as follow-up.

Never touches the canonical production database.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_disposable_e2e_p66 import DryRunExecutionClient, _run
from tradehub.autonomy import policy as autonomy_policy
from tradehub_research.config import ResearchSettings
from tradehub_research.ops.decision_pipeline import (
    AUTHORITY_SCHEMA_VERSION,
    _publish_authority,
)
from tradehub_research.portfolio.policy import PolicyStatus, load_policy_from_json
from tradehub_research.portfolio.sizing import size_buy, size_sell
from tradehub_research.screens import canonical_json

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tradehub_research" / "policy_specs" / "paper-provisional-v1.json"
SHARE = 1_000_000
HELD = 40 * SHARE  # 40 shares held
NAV = 50_000_000_000  # $50,000


def _policy():
    return load_policy_from_json(
        "paper-provisional-v1", PolicyStatus.PROVISIONAL, SPEC.read_text(encoding="utf-8")
    )


def _buy(current=HELD):
    return size_buy(
        _policy(),
        conviction_ppm=900_000,
        data_quality_ppm=900_000,
        agreement_ppm=900_000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=NAV,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=SHARE,
        clips={},
        available_cash_microusd=NAV,
        current_quantity_microunits=current,
        min_action_notional_microusd=0,
    )


def _sell(current=HELD, sellable=None, full_exit=False):
    return size_sell(
        _policy(),
        current_weight_ppm=100_000,
        current_quantity_microunits=current,
        sellable_quantity_microunits=current if sellable is None else sellable,
        mark_price_microusd=50_000_000,
        nav_microusd=NAV,
        quantity_increment_microunits=SHARE,
        full_exit=full_exit,
        min_action_notional_microusd=0,
    )


# --------------------------------------------------------------------------
# the PRODUCER: the real sizing code is delta/target consistent
# --------------------------------------------------------------------------
def test_real_size_buy_completion_is_target_holding_not_order_size():
    result = _buy()
    assert result.max_quantity_microunits > 0, result.reason
    # completion is the TARGET holding: current + the delta actually ordered
    assert result.completion_quantity_microunits == HELD + result.max_quantity_microunits, (
        result.to_dict()
    )
    assert result.completion_quantity_microunits != result.max_quantity_microunits


def test_real_size_sell_completion_is_target_holding_not_order_size():
    result = _sell()
    assert result.max_quantity_microunits > 0, result.reason
    assert result.completion_quantity_microunits == HELD - result.max_quantity_microunits, (
        result.to_dict()
    )
    assert result.completion_quantity_microunits != result.max_quantity_microunits


def test_real_size_sell_full_exit_completes_flat():
    result = _sell(full_exit=True)
    assert result.completion_quantity_microunits == 0
    assert result.max_quantity_microunits == HELD


# --------------------------------------------------------------------------
# the CONSUMER: the production runner sends the DELTA
# --------------------------------------------------------------------------
def _identity(envelope: dict) -> str:
    stable = {k: v for k, v in envelope.items() if k != "exported_at"}
    return hashlib.sha256(canonical_json(stable).encode()).hexdigest()


def _runner_case(tmp_path, *, action: str, current: int, order: int, completion: int, state):
    """Build envelope + authority from REAL sizing output and run production."""
    envelope = {
        "schema_version": "paper-proposal-envelope-v1",
        "proposal": {
            "proposal_id": f"held-{action.lower()}-1",
            "security_id": "0000320193",
            "action": action,
            "max_quantity_microunits": order,
            "completion_quantity_microunits": completion,
            "max_notional_microusd": 10_000_000_000,
            "target_weight_ppm": 90_000,
            "current_weight_ppm": 10_000,
            "current_quantity_microunits": current,
            "sellable_quantity_microunits": current,
            "mark_price_microusd": 50_000_000,
            "score_snapshot_id": "score-1",
            "portfolio_snapshot_id": "pf-1",
            "policy_version": "paper-provisional-v1",
            "sizing_policy_version": "paper-sizing-v1",
            "current_state": state[0],
            "proposed_state": state[1],
            "quantity_increment_microunits": SHARE,
            "limit_only": True,
            "created_at": "2025-06-09T00:00:00Z",
        },
        "symbol": "AAPL",
        "data_as_of": "2025-06-09",
        "universe": "US_STOCKS",
        "exported_at": "2025-06-09T00:05:00Z",
    }
    authority_dir = tmp_path / "authority"
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
        "created_at": "2025-06-09T00:00:00Z",
    }
    _publish_authority(authority_dir, record)

    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{proposal['proposal_id']}.json").write_text(json.dumps(envelope))

    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    policy_file = policy_dir / "paper_policy.json"
    policy_file.write_text(json.dumps(autonomy_policy.default_policy_payload()))

    moment = datetime.fromisoformat("2025-06-09T00:00:00+00:00") + timedelta(seconds=60)
    d = {
        "inbox": inbox,
        "authority_dir": authority_dir,
        "policy_path": policy_file,
        "ledger": tmp_path / "ledger.jsonl",
        "budget_db": tmp_path / "budget.sqlite",
        "settings": ResearchSettings(api_token="test-token"),
        "client": DryRunExecutionClient(),
    }
    summary = _run(d, now=moment.replace(tzinfo=timezone.utc))
    payloads = [p for path, p in d["client"].calls if path == "/orders/preview"]
    return summary, payloads


def test_real_chain_add_preview_sends_the_delta(tmp_path):
    """ADD: 40 held, real sizing buys a non-zero delta, completion = 40 + delta."""
    result = _buy()
    order, completion = result.max_quantity_microunits, result.completion_quantity_microunits
    assert completion != order, "need a distinguishing case"

    summary, payloads = _runner_case(
        tmp_path,
        action="BUY",
        current=HELD,
        order=order,
        completion=completion,
        state=("HOLD", "ADD"),
    )
    assert summary["orders"] == 1, summary["refusals"]
    assert len(payloads) == 1
    assert payloads[0]["quantity"] == order // SHARE, (
        f"sent {payloads[0]['quantity']} shares; expected delta {order // SHARE} "
        f"(completion was {completion // SHARE})"
    )


def test_real_chain_trim_preview_sends_the_delta(tmp_path):
    """TRIM: 40 held, real sizing sells a non-zero delta, completion = 40 - delta."""
    result = _sell()
    order, completion = result.max_quantity_microunits, result.completion_quantity_microunits
    assert completion != order, "need a distinguishing case"

    summary, payloads = _runner_case(
        tmp_path,
        action="SELL",
        current=HELD,
        order=order,
        completion=completion,
        state=("HOLD", "TRIM"),
    )
    assert summary["orders"] == 1, summary["refusals"]
    assert len(payloads) == 1
    assert payloads[0]["quantity"] == order // SHARE, (
        f"sent {payloads[0]['quantity']} shares; expected delta {order // SHARE} "
        f"(completion was {completion // SHARE})"
    )

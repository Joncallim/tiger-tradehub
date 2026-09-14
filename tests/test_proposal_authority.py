"""Proposal-authority projection boundary tests.

Autonomy must NOT read research.db. Authority is the narrow, research-written,
atomically published projection that binds an inbox envelope to the persisted
research proposal. A valid-shaped envelope without matching authority fails
closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tradehub.autonomy.runner import AutonomyRefusal, _validate_proposal_authority
from tradehub_research.ops.decision_pipeline import (
    AUTHORITY_SCHEMA_VERSION,
    _publish_authority,
)
from tradehub_research.screens import canonical_json

PROPOSAL_FIELDS = {
    "security_id": "0000320193",
    "action": "BUY",
    "max_quantity_microunits": 1_000_000_000,
    "completion_quantity_microunits": 1_000_000_000,
    "max_notional_microusd": 500_000_000,
    "target_weight_ppm": 50_000,
    "current_weight_ppm": 0,
    "score_snapshot_id": "score-1",
    "portfolio_snapshot_id": "snap-1",
    "policy_version": "paper-provisional-v1",
    "sizing_policy_version": "paper-sizing-v1",
    # Existing owner-approved PAPER autonomy state machine.
    "current_state": "WATCH",
    "proposed_state": "ENTER",
}


def _envelope(**overrides) -> dict:
    proposal = {"proposal_id": "prop-1", **PROPOSAL_FIELDS, **overrides}
    return {
        "schema_version": "paper-proposal-envelope-v1",
        "proposal": proposal,
        "symbol": "AAPL",
        "universe": "US_STOCKS",
        "data_as_of": "2026-09-10",
        "exported_at": "2026-09-14T00:00:00Z",
    }


def _identity(envelope: dict) -> str:
    stable = dict(envelope)
    stable.pop("exported_at", None)
    return hashlib.sha256(canonical_json(stable).encode()).hexdigest()


def _publish(authority_dir: Path, envelope: dict, **overrides) -> dict:
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
        "current_state": proposal["current_state"],
        "proposed_state": proposal["proposed_state"],
        "state_transition": f"{proposal['current_state']}->{proposal['proposed_state']}",
        "requires_human_approval": 0,
        "autonomy_eligible": True,
        "data_as_of": envelope["data_as_of"],
        "envelope_identity_hash": _identity(envelope),
        "created_at": "2026-09-14T00:00:00Z",
    }
    record.update(overrides)
    # Keep the record internally consistent unless a test deliberately
    # overrides the convenience string to prove the consistency check fires.
    if "state_transition" not in overrides:
        record["state_transition"] = f"{record['current_state']}->{record['proposed_state']}"
    _publish_authority(authority_dir, record)
    return record


def test_exact_match_is_accepted(tmp_path):
    envelope = _envelope()
    _publish(tmp_path, envelope)
    record = _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)
    assert record["proposal_id"] == "prop-1"
    assert record["canonical_symbol"] == "AAPL"


def test_absent_authority_fails_closed(tmp_path):
    with pytest.raises(AutonomyRefusal, match="no persisted proposal authority"):
        _validate_proposal_authority(tmp_path, _envelope(), fixture_mode=False)


def test_unavailable_authority_directory_fails_closed():
    with pytest.raises(AutonomyRefusal, match="authority directory unavailable"):
        _validate_proposal_authority(None, _envelope(), fixture_mode=False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"canonical_symbol": "MSFT"},
        {"action": "SELL"},
        {"max_quantity_microunits": 999},
        {"completion_quantity_microunits": 999},
        {"max_notional_microusd": 1},
        {"target_weight_ppm": 999_999},
        {"policy_version": "other-policy"},
        {"sizing_policy_version": "other-sizing"},
    ],
)
def test_authority_mismatch_fails_closed(tmp_path, overrides):
    envelope = _envelope()
    _publish(tmp_path, envelope, **overrides)
    with pytest.raises(AutonomyRefusal):
        _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)


def test_tampered_envelope_identity_fails_closed(tmp_path):
    envelope = _envelope()
    _publish(tmp_path, envelope)
    tampered = _envelope(max_notional_microusd=1_000_000)  # envelope changed after publish
    with pytest.raises(AutonomyRefusal, match="identity does not match"):
        _validate_proposal_authority(tmp_path, tampered, fixture_mode=False)


def test_ineligible_authority_fails_closed(tmp_path):
    envelope = _envelope()
    _publish(tmp_path, envelope, autonomy_eligible=False)
    with pytest.raises(AutonomyRefusal, match="not eligible PAPER/non-FIXTURE"):
        _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)


def test_wrong_authority_schema_fails_closed(tmp_path):
    envelope = _envelope()
    _publish(tmp_path, envelope, schema_version="paper-proposal-authority-v0")
    with pytest.raises(AutonomyRefusal, match="unexpected proposal authority schema"):
        _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)


def test_authority_id_mismatch_fails_closed(tmp_path):
    envelope = _envelope()
    record = _publish(tmp_path, envelope)
    record["proposal_id"] = "someone-else"  # file name still prop-1.json
    (tmp_path / "prop-1.json").write_text(json.dumps(record, sort_keys=True))
    with pytest.raises(AutonomyRefusal, match="authority id mismatch"):
        _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)


def test_authority_publication_is_idempotent_and_never_rewritten(tmp_path):
    envelope = _envelope()
    first = _publish(tmp_path, envelope)
    path = tmp_path / "prop-1.json"
    original = path.read_text()
    second = _publish(tmp_path, envelope)
    assert first == second
    assert path.read_text() == original


def test_authority_collision_is_refused(tmp_path):
    envelope = _envelope()
    _publish(tmp_path, envelope)
    with pytest.raises(ValueError, match="authority collision"):
        _publish(tmp_path, envelope, max_notional_microusd=2)


def test_authority_published_before_envelope_records_no_evidence_or_credentials(tmp_path):
    envelope = _envelope()
    _publish(tmp_path, envelope)
    record = json.loads((tmp_path / "prop-1.json").read_text())
    # The projection must stay narrow: no evidence, no prose, no secrets.
    forbidden = {"evidence", "claims", "thesis", "token", "api_key", "secret"}
    assert not (set(record) & forbidden)
    assert record["envelope_identity_hash"] == _identity(envelope)


@pytest.mark.parametrize(
    ("field", "tampered"),
    [("current_state", "TRIM"), ("proposed_state", "EXIT")],
)
def test_state_field_mismatch_is_refused(tmp_path, field, tampered):
    """The authority record binds the proposal's state-machine fields."""
    envelope = _envelope()
    _publish(tmp_path, envelope, **{field: tampered})
    with pytest.raises(AutonomyRefusal, match=field):
        _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)


def test_state_transition_is_recorded_in_authority(tmp_path):
    envelope = _envelope()
    record = _publish(tmp_path, envelope)
    assert record["current_state"] == "WATCH"
    assert record["proposed_state"] == "ENTER"
    assert record["state_transition"] == "WATCH->ENTER"


def test_internally_inconsistent_state_transition_is_refused(tmp_path):
    """A producer bug writing a mismatched convenience string must be caught.

    The runner recomputes the gate from the record's own bound fields, so the
    stored string must agree with them or the record is refused outright.
    """
    envelope = _envelope()  # fields say WATCH->ENTER
    _publish(tmp_path, envelope, state_transition="HOLD->EXIT")
    with pytest.raises(AutonomyRefusal, match="internally inconsistent"):
        _validate_proposal_authority(tmp_path, envelope, fixture_mode=False)


def test_marked_fixture_bypasses_authority(tmp_path):
    assert _validate_proposal_authority(tmp_path, _envelope(), fixture_mode=True) == {}


def test_production_mode_refuses_an_unmarked_envelope(tmp_path):
    """Production: no authority record, no execution. Fixture mode is the only
    way past, and it can never be requested by the envelope itself."""
    with pytest.raises(AutonomyRefusal, match="no persisted proposal authority"):
        _validate_proposal_authority(tmp_path, _envelope(), fixture_mode=False)

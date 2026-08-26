"""Portfolio policy contract: validation, registry, status gating."""

from __future__ import annotations

import json

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.fixtures import (
    FIXTURE_POLICY_VERSION,
    fixture_policy,
    fixture_policy_spec,
)
from tradehub_research.portfolio.policy import (
    PolicyRegistry,
    build_policy,
    load_policy_from_json,
    validate_spec,
)
from tradehub_research.portfolio.types import PolicyStatus


def test_fixture_policy_validates_and_hashes_stably():
    policy = fixture_policy()
    assert policy.policy_version == FIXTURE_POLICY_VERSION
    assert policy.policy_status == PolicyStatus.FIXTURE
    assert policy.spec_hash == policy.spec_hash
    again = fixture_policy()
    assert again.spec_json == policy.spec_json
    assert again.spec_hash == policy.spec_hash


def test_policy_rejects_extra_top_level_keys():
    spec = fixture_policy_spec()
    spec["extra"] = 1
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_rejects_missing_required_key():
    spec = fixture_policy_spec()
    del spec["risk"]
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_rejects_floats():
    spec = fixture_policy_spec()
    spec["sizing"]["trim_remaining_fraction_ppm"] = 0.5
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_rejects_unsorted_bands():
    spec = fixture_policy_spec()
    spec["sizing"]["conviction_bands"] = [
        {"min_ppm": 0, "base_target_ppm": 0},
        {"min_ppm": 700000, "base_target_ppm": 80000},
    ]
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_rejects_duplicate_band_minima():
    spec = fixture_policy_spec()
    spec["sizing"]["quality_bands"] = [
        {"min_ppm": 700000, "multiplier_ppm": 1000000},
        {"min_ppm": 700000, "multiplier_ppm": 750000},
        {"min_ppm": 0, "multiplier_ppm": 500000},
    ]
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_requires_persistence_on_action_edges():
    spec = fixture_policy_spec()
    spec["transition_controls"]["WATCH_ENTER"]["required_evidence_observations"] = 0
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_rejects_non_canonical_edge():
    spec = fixture_policy_spec()
    spec["eligibility_rules"][0]["to_state"] = "HOLD"  # DISCOVER->HOLD is not canonical
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_policy_rejects_duplicate_rule_ids_and_priorities():
    spec = fixture_policy_spec()
    spec["eligibility_rules"].append(dict(spec["eligibility_rules"][0]))
    with pytest.raises(ValueError):
        validate_spec(spec)
    spec = fixture_policy_spec()
    spec["eligibility_rules"][1]["rule_id"] = "watch-on-score-band"
    with pytest.raises(ValueError):
        validate_spec(spec)


def test_paper_policy_requires_approval():
    spec = fixture_policy_spec()
    with pytest.raises(ValueError):
        build_policy("paper-v1", PolicyStatus.PAPER, spec)
    # a PAPER policy must not carry FIXTURE verification methods; strip them
    paper_spec = json.loads(json.dumps(spec))
    paper_spec["thesis_break"]["allowed_verification_methods"] = [
        "OWNER_ATTESTED",
        "DETERMINISTIC_RULE",
    ]
    policy = build_policy(
        "paper-v1",
        PolicyStatus.PAPER,
        paper_spec,
        approved_by="owner",
        approved_at="2025-01-01T00:00:00Z",
    )
    assert policy.policy_status == PolicyStatus.PAPER
    # FIXTURE methods inside a PAPER policy are rejected
    with pytest.raises(ValueError, match="FIXTURE"):
        build_policy(
            "paper-v2",
            PolicyStatus.PAPER,
            spec,
            approved_by="owner",
            approved_at="2025-01-01T00:00:00Z",
        )


def test_registry_register_get_and_collision(tmp_path):
    database = ResearchDB(tmp_path / "policy.db")
    database.migrate()
    registry = PolicyRegistry(database)
    policy = fixture_policy()
    registry.register(policy)
    loaded = registry.get(FIXTURE_POLICY_VERSION)
    assert loaded.spec_hash == policy.spec_hash
    assert loaded.spec_json == policy.spec_json
    # idempotent re-register
    registry.register(policy)
    # byte mismatch on same version rejected
    other = build_policy(FIXTURE_POLICY_VERSION, PolicyStatus.FIXTURE, fixture_policy_spec())
    assert other.spec_json == policy.spec_json  # identical content
    modified = fixture_policy_spec()
    modified["budget"]["max_actionable_count"] = 99
    with pytest.raises(ValueError):
        registry.register(build_policy(FIXTURE_POLICY_VERSION, PolicyStatus.FIXTURE, modified))
    with pytest.raises(KeyError):
        registry.get("missing-version")


def test_registry_unknown_version_fails_closed(tmp_path):
    database = ResearchDB(tmp_path / "policy.db")
    database.migrate()
    with pytest.raises(KeyError):
        PolicyRegistry(database).get("never-registered")


def test_load_policy_from_json_roundtrip():
    raw = json.dumps(fixture_policy_spec())
    policy = load_policy_from_json("v-json", PolicyStatus.FIXTURE, raw)
    assert policy.spec_hash == fixture_policy().spec_hash

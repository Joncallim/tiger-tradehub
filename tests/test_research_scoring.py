from __future__ import annotations

import pytest

from tradehub_research.committee.scoring import classify_trajectory, score_screens
from tradehub_research.committee.store import ScoringSpec


def _screen(family: str, *, quality: float = 1, evidence: list[str] | None = None, **changes):
    row = {
        "family": family,
        "sufficient_data": True,
        "passed": True,
        "data_quality": quality,
        "reason_codes": [],
        "evidence_ids": evidence or [],
    }
    row.update(changes)
    return row


def _evidence(
    evidence_id: str,
    group: str,
    *,
    source_id: str | None = "sec",
    clusters: list[str] | None = None,
    record_type: str = "news",
):
    return {
        "evidence_id": evidence_id,
        "content_hash": evidence_id + "-hash",
        "source_id": source_id,
        "underlying_group": group,
        "cluster_ids": clusters or [],
        "record_type": record_type,
        "public_available_time": "2025-01-01Z",
        "supersedes_evidence_id": None,
    }


def test_exact_arithmetic_penalties_bonus_and_half_up():
    screens = [
        _screen("valuation", quality=0.5, evidence=["a"]),
        _screen("inflection", evidence=["b"], reason_codes=["stale_source"]),
        _screen("quality", sufficient_data=False, passed=False, data_quality=0),
        _screen("informed_activity", sufficient_data=False, passed=False, data_quality=0),
        _screen("event", sufficient_data=False, passed=False, data_quality=0),
        _screen("momentum_confirmation", sufficient_data=False, passed=False, data_quality=0),
    ]
    result = score_screens(
        screens,
        [_evidence("a", "g1", source_id="one"), _evidence("b", "g2", source_id="two")],
        ScoringSpec().as_dict(),
    )
    assert result["base_evidence"] == 36
    assert result["confluence_bonus"] == 5
    assert result["penalties"] == {"low_quality": 9.0, "missing": 6.0, "staleness": 2.0}
    assert result["raw_score"] == 24
    assert result["conviction"] == 25


def test_one_xbrl_source_across_three_families_is_one_independence_unit():
    screens = [
        _screen("valuation", evidence=["v"]),
        _screen("inflection", evidence=["i"]),
        _screen("quality", evidence=["q"]),
    ]
    evidence = [
        _evidence(item, "xbrl:sec:accession", record_type="xbrl_fact")
        for item in ("v", "i", "q")
    ]
    result = score_screens(screens, evidence, ScoringSpec().as_dict())
    assert result["underlying_groups"] == ['independence:v1:["sec"]']
    assert result["confluence_bonus"] == 0
    assert result["raw_score"] == 50  # 54 base - 4 missing event/activity


def test_unclustered_rows_from_one_source_are_one_independence_unit():
    screens = [_screen("valuation", evidence=["a"]), _screen("quality", evidence=["b"])]
    evidence = [_evidence("a", "event:same:a"), _evidence("b", "event:same:b")]
    result = score_screens(screens, evidence, ScoringSpec().as_dict())
    assert result["underlying_groups"] == ['independence:v1:["sec"]']
    assert result["confluence_bonus"] == 0


def test_distinct_unlinked_sources_earn_bounded_confluence_bonus():
    screens = [
        _screen("valuation", evidence=["a"]),
        _screen("inflection", evidence=["b"]),
        _screen("quality", evidence=["c"]),
        _screen("event", evidence=["d"]),
    ]
    evidence = [
        _evidence(item, f"event:{source}:{item}", source_id=source)
        for item, source in zip(
            ("a", "b", "c", "d"), ("one", "two", "three", "four"), strict=True
        )
    ]
    result = score_screens(screens, evidence, ScoringSpec().as_dict())
    assert len(result["underlying_groups"]) == 4
    assert result["confluence_bonus"] == 10


def test_distinct_sources_sharing_cluster_collapse_to_one_unit():
    screens = [_screen("valuation", evidence=["a"]), _screen("quality", evidence=["b"])]
    evidence = [
        _evidence("a", "event:one:a", source_id="one", clusters=["same-event"]),
        _evidence("b", "event:two:b", source_id="two", clusters=["same-event"]),
    ]
    result = score_screens(screens, evidence, ScoringSpec().as_dict())
    assert result["underlying_groups"] == ['independence:v1:["one","two"]']
    assert result["confluence_bonus"] == 0


def test_missing_source_id_is_not_a_confluence_unit():
    screens = [_screen("valuation", evidence=["a"]), _screen("quality", evidence=["b"])]
    evidence = [
        _evidence("a", "event:one:a", source_id="one"),
        _evidence("b", "event:missing:b", source_id=None),
    ]
    result = score_screens(screens, evidence, ScoringSpec().as_dict())
    assert result["underlying_groups"] == ['independence:v1:["one"]']
    assert result["confluence_bonus"] == 0


def test_duplicate_scored_family_is_rejected():
    with pytest.raises(ValueError, match="duplicate scored screen family"):
        score_screens([_screen("valuation"), _screen("valuation")], [], ScoringSpec().as_dict())


def test_trajectory_four_causes_and_direction_labels():
    current = {
        "scoring_config_hash": "v1",
        "scored_evidence_hash": "new",
        "conviction": 65,
    }
    assert classify_trajectory(
        None,
        current,
        screen_hashes_equal=False,
        committee_hashes_differ=False,
        correction_chain=False,
    ) == {"change_cause": "INITIAL", "trajectory_label": "INITIAL", "delta": None}
    prior = {
        "scoring_config_hash": "old",
        "scored_evidence_hash": "old",
        "conviction": 70,
    }
    assert (
        classify_trajectory(
            prior,
            current,
            screen_hashes_equal=False,
            committee_hashes_differ=False,
            correction_chain=False,
        )["trajectory_label"]
        == "REBASED"
    )
    prior.update(scoring_config_hash="v1", scored_evidence_hash="new", conviction=65)
    assert classify_trajectory(
        prior,
        current,
        screen_hashes_equal=True,
        committee_hashes_differ=True,
        correction_chain=False,
    ) == {"change_cause": "MODEL_REASSESSMENT", "trajectory_label": "STABLE", "delta": 0}
    prior.update(scored_evidence_hash="old", conviction=70)
    correction = classify_trajectory(
        prior,
        current,
        screen_hashes_equal=False,
        committee_hashes_differ=False,
        correction_chain=True,
    )
    assert correction == {
        "change_cause": "CORRECTION_RESTATEMENT",
        "trajectory_label": "FALLING",
        "delta": -5,
    }
    current["conviction"] = 75
    evidence = classify_trajectory(
        prior,
        current,
        screen_hashes_equal=False,
        committee_hashes_differ=False,
        correction_chain=False,
    )
    assert evidence == {
        "change_cause": "EVIDENCE_DRIVEN",
        "trajectory_label": "RISING",
        "delta": 5,
    }


def test_screen_only_change_is_an_explicit_rebase():
    prior = {"scoring_config_hash": "v1", "scored_evidence_hash": "same", "conviction": 50}
    current = {"scoring_config_hash": "v1", "scored_evidence_hash": "same", "conviction": 55}
    assert classify_trajectory(
        prior,
        current,
        screen_hashes_equal=False,
        committee_hashes_differ=False,
        correction_chain=False,
    ) == {
        "change_cause": "SCREEN_METHODOLOGY_CHANGE",
        "trajectory_label": "REBASED",
        "delta": None,
    }

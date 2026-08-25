from __future__ import annotations

from copy import deepcopy

import pytest

from tradehub_research.committee.assessment import AssessmentValidationError, validate_assessment
from tradehub_research.committee.comparator import compare_assessments
from tradehub_research.committee.store import ComparatorSpec

SPEC = ComparatorSpec().as_dict()


def claim(
    key="valuation_vs_history",
    direction="bullish",
    materiality=3,
    cited=None,
    contradictory=None,
    kind="fact",
):
    return {
        "claim_key": key,
        "claim_type": kind,
        "direction": direction,
        "statement": "statement",
        "materiality": materiality,
        "uncertainty": 0.2,
        "cited_evidence_ids": cited if cited is not None else ["e1"],
        "contradictory_evidence_ids": contradictory or [],
        "falsification_condition": "condition" if kind == "projection" else None,
    }


def test_comparator_five_buckets_are_disjoint_and_replayable():
    a = {
        "claims": [
            claim(cited=["conflict"]),
            claim("earnings_quality", cited=[]),
            claim("margin_durability", cited=["same"]),
            claim("revenue_inflection", cited=["a"]),
            claim("other", materiality=2),
        ]
    }
    b = {
        "claims": [
            claim(direction="bearish", cited=["conflict"]),
            claim("margin_durability", cited=["same"]),
            claim("revenue_inflection", cited=["b"]),
            claim("event_repricing"),
        ]
    }
    first = compare_assessments(a, b, SPEC)
    assert first == compare_assessments(a, b, SPEC)
    ids = [item["item_id"] for bucket in first["buckets"].values() for item in bucket]
    assert len(ids) == len(set(ids))
    assert first["buckets"]["EVIDENCE_CONFLICT"]
    assert first["buckets"]["UNSUPPORTED"]
    assert {item["alignment"] for item in first["buckets"]["SHARED"]} == {"aligned", "disjoint"}
    assert first["buckets"]["OMITTED"]


def test_comparator_r1_r2_r3_r4_and_null_agreement():
    empty = compare_assessments({"claims": []}, {"claims": []}, SPEC)
    assert empty["agreement"] is None and empty["triggers"] == ["R3"]
    r1 = compare_assessments(
        {"claims": [claim()]}, {"claims": [claim(direction="bearish", cited=["e2"])]}, SPEC
    )
    assert "R1" in r1["triggers"]
    r2 = compare_assessments(
        {"claims": [claim(cited=["e1"])]},
        {"claims": [claim(direction="bearish", cited=["e1"])]},
        SPEC,
    )
    assert "R2" in r2["triggers"]
    keys = ["valuation_vs_history", "earnings_quality", "margin_durability"]
    r3 = compare_assessments(
        {"claims": [claim(key, cited=["a"]) for key in keys]},
        {"claims": [claim(key, cited=["b"]) for key in keys]},
        SPEC,
    )
    assert "R3" in r3["triggers"]
    r4 = compare_assessments(
        {
            "claims": [
                claim("valuation_vs_history"),
                claim("earnings_quality"),
                claim("margin_durability"),
            ]
        },
        {"claims": [claim("valuation_vs_history")]},
        SPEC,
    )
    assert "R4" in r4["triggers"]


def payload():
    return {
        "candidate_id": "candidate",
        "pack_hash": "pack",
        "role": "neutral_analyst_a",
        "provider": "p",
        "model_id": "m",
        "prompt_version": "v1",
        "assessment_schema_version": 1,
        "taxonomy_version": 1,
        "model_route": "route",
        "billing_class": "local",
        "claims": [claim()],
        "cited_evidence_ids": ["e1"],
        "missing_evidence": [],
        "thesis": {
            "summary": "s",
            "upside_mechanism": "u",
            "downside_mechanism": "d",
            "thesis_break_conditions": [],
        },
        "confidence": 0.5,
        "uncertainty": 0.5,
        "usage": {},
        "cost": {},
        "evaluation_time": "2025-01-01Z",
        "submitted_at": "2025-01-02Z",
    }


RUN = {
    "candidate_id": "candidate",
    "pack_hash": "pack",
    "assessment_schema_version": 1,
    "prompt_versions_json": '{"neutral":"v1"}',
}
PACK = {"run": {"as_of": "2025-01-01Z"}, "evidence": [{"evidence_id": "e1"}]}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(extra=True),
        lambda p: p["claims"].append(deepcopy(p["claims"][0])),
        lambda p: p.update(confidence=float("nan")),
        lambda p: p["claims"][0].update(statement="x" * 513),
        lambda p: p.update(pack_hash="wrong"),
        lambda p: (
            p["claims"][0].update(cited_evidence_ids=["bad"]),
            p.update(cited_evidence_ids=["bad"]),
        ),
        lambda p: (p["claims"][0].update(cited_evidence_ids=[]), p.update(cited_evidence_ids=[])),
        lambda p: p["claims"][0].update(claim_key="other", materiality=3),
        lambda p: p["claims"][0].update(claim_type="projection", falsification_condition=None),
    ],
)
def test_assessment_strict_rejections(mutation):
    value = payload()
    mutation(value)
    with pytest.raises(AssessmentValidationError):
        validate_assessment(
            value, run=RUN, pack_body=PACK, comparator_spec=SPEC, expected_role="neutral_analyst_a"
        )

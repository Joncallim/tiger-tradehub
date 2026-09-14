"""#67 review round 5 regressions.

Round 5 found no P0/P1 (all six round-4 remediations verified real) but three
defects in the model-facing honesty counters and one non-discriminating
regression. This module pins the counter fixes:

1. a series screen's advertised ``evidence_ids`` is deduplicated and reconciled
   against the screen's declared set, so ``evidence_ids_omitted`` can never
   understate and can never go negative;
2. every aggregate's counters add up after admission (``compacted`` is recomputed
   from the admitted set, not left at its pre-trim value);
3. the top-level series counters are distinct-unit counts that reconcile:
   presented + omitted + not-frozen == referenced.
"""

from __future__ import annotations

from test_committee_identity_migration import (  # noqa: E402 - sibling test fixture
    _fixture as identity_fixture,
)

from tradehub_research.committee import view as view_module
from tradehub_research.committee.view import CommitteeViewBuilder


def _assert_counters_reconcile(view: dict) -> None:
    omitted = view["evidence_omitted"]
    assert (
        omitted["series_representatives_presented"]
        + omitted["series_observations_omitted"]
        + omitted["series_references_not_frozen"]
        == omitted["series_observations_referenced_distinct"]
    )
    presented = {row["evidence_id"] for row in view["evidence"]}
    for screen in view["screens"]:
        advertised = screen["evidence_ids"]
        assert len(advertised) == len(set(advertised)), "advertised ids must be deduplicated"
        assert set(advertised) <= presented
        assert screen["evidence_ids_omitted"] == screen["evidence_id_count"] - len(advertised)
        assert screen["evidence_ids_omitted"] >= 0
        for entry in screen["series_aggregates"]:
            assert (
                entry["observations_presented"]
                + entry["observations_compacted"]
                + entry["observations_not_frozen"]
                == entry["observation_count"]
            )
            assert entry["observations_omitted"] == (
                entry["observations_compacted"] + entry["observations_not_frozen"]
            )


def test_advertised_evidence_ids_are_deduplicated_and_reconciled(tmp_path):
    """Review finding P2 (round 5): duplicates and a negative/wrong omitted count.

    The fixture declares one series twice (``adv_20d`` and ``eligible_bar_count``
    over the same observations) with only 12 declared ids, which is exactly the
    shape that produced duplicate advertised ids and an understated count.
    """
    database, candidate_id = identity_fixture(tmp_path / "dedupe.db")
    view = CommitteeViewBuilder(database).build(candidate_id).body
    _assert_counters_reconcile(view)
    momentum = next(
        screen for screen in view["screens"] if screen["family"] == "momentum_confirmation"
    )
    assert momentum["evidence_ids"], "the series screen must advertise representatives"
    assert momentum["evidence_id_count"] == 12
    assert momentum["evidence_ids_omitted"] == 12 - len(momentum["evidence_ids"])
    # Two features over the same series must not double the advertised list.
    assert len(momentum["evidence_ids"]) <= 4


def test_counters_still_reconcile_when_admission_declines(tmp_path, monkeypatch):
    """Review finding P3 (round 5): the counters must add up in the decline path."""
    database, candidate_id = identity_fixture(tmp_path / "decline.db")
    monkeypatch.setattr(view_module, "MAX_VIEW_EVIDENCE_ROWS", 3)
    view = CommitteeViewBuilder(database).build(candidate_id).body
    _assert_counters_reconcile(view)
    assert view["evidence_omitted"]["series_observations_omitted"] > 0
    # A representative declined by admission must appear in the compacted count,
    # not vanish from the arithmetic.
    for screen in view["screens"]:
        for entry in screen["series_aggregates"]:
            if entry["observations_not_frozen"] == 0:
                assert entry["observations_compacted"] == entry["observations_omitted"]

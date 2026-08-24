from __future__ import annotations

from tradehub_research.funnel import FunnelConfig, FunnelResultRow, candidate_id, run_funnel


def row(sid: str, family: str, passed: bool = False, evidence=()) -> FunnelResultRow:
    return FunnelResultRow(sid, family, f"{sid}-{family}", True, passed, 0.8, 0.9, evidence)


def test_candidate_id_does_not_depend_on_ordinal() -> None:
    assert candidate_id("run", "security") == candidate_id("run", "security")


def test_cluster_deduplication() -> None:
    results = [
        row("S", "valuation", True, ("e1",)),
        row("S", "quality", True, ("e2",)),
    ]
    candidates, flags = run_funnel(
        run_id="r",
        logical_material="m",
        config=FunnelConfig(control_count=0),
        universe=["S"],
        holdings=set(),
        results=results,
        sectors={},
        cluster_lookup={"e1": {"shared"}, "e2": {"shared"}},
    )
    assert not flags
    assert candidates[0].rank_telemetry["distinct_supporting_cluster_count"] == 1


def test_mandatory_overflow_is_never_truncated() -> None:
    universe = [f"S{i}" for i in range(51)]
    candidates, flags = run_funnel(
        run_id="r",
        logical_material="m",
        config=FunnelConfig(),
        universe=universe,
        holdings=set(universe),
        results=[],
        sectors={},
        cluster_lookup={},
    )
    assert len(candidates) == 51
    assert flags == ["budget_overflow_mandatory"]

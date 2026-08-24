"""RA-01: offline qualification for Phase-1 Hunters and candidate funnel."""

from __future__ import annotations

import ast
import hashlib
import inspect
from dataclasses import replace
from pathlib import Path

from tradehub_research.funnel import FunnelConfig, FunnelResultRow, control_key, run_funnel
from tradehub_research.hunters import (
    event,
    inflection,
    informed_activity,
    momentum,
    valuation,
)
from tradehub_research.screens import ScreenContext, canonical_json, registered_screens


def _context(**changes) -> ScreenContext:
    values = dict(
        facts={},
        price_bars={},
        form4={},
        identity_events={},
        market_caps={},
        universe=["S"],
        as_of="2025-04-01T00:00:00Z",
        sectors={"S": "Technology"},
    )
    values.update(changes)
    return ScreenContext(**values)


def _row(sid: str, family: str, *, passed=False, sufficient=True, evidence=()):
    return FunnelResultRow(sid, family, f"r-{sid}-{family}", sufficient, passed, 0.8, 0.9, evidence)


def contract_all_screens(tmp: Path) -> None:
    screens = registered_screens()
    assert len(screens) == 6
    assert [s.family for s, _ in screens] == sorted(s.family for s, _ in screens)
    assert all("option" not in s.screen_id for s, _ in screens)
    for _spec, fn in screens:
        assert fn(_context(), "S") == fn(_context(), "S")
        assert tuple(fn(_context(), "S").evidence_ids) == tuple(
            sorted(set(fn(_context(), "S").evidence_ids))
        )


def spec_hash_and_collision(tmp: Path) -> None:
    spec = valuation.SCREEN_SPEC
    assert spec.config_hash == hashlib.sha256(spec.canonical_json().encode()).hexdigest()
    assert (
        replace(spec, parameters={**spec.parameters, "min_earnings_yield": 0.06}).config_hash
        != spec.config_hash
    )
    assert replace(spec, feature_schema_version=2).config_hash != spec.config_hash
    assert replace(spec, implementation_id="different").config_hash != spec.config_hash


def population_tristate(tmp: Path) -> None:
    missing = event.evaluate(_context(identity_feed_complete=False), "S")
    weak = event.evaluate(_context(identity_feed_complete=True), "S")
    strong = event.evaluate(
        _context(
            identity_feed_complete=True,
            identity_events={
                "S": [
                    {
                        "id": 1,
                        "event_type": "ticker_change",
                        "public_available_time": "2025-03-20T00:00:00Z",
                    }
                ]
            },
        ),
        "S",
    )
    assert [(x.sufficient_data, x.passed) for x in (strong, weak, missing)] == [
        (True, True),
        (True, False),
        (False, False),
    ]
    assert "evaluated" not in inspect.getsource(
        __import__("tradehub_research.screens", fromlist=["ScreenResult"]).ScreenResult
    )


def raw_feature_lineage(tmp: Path) -> None:
    result = valuation.evaluate(_context(), "S")
    encoded = canonical_json(result.raw_features)
    assert '"value":null' in encoded and '"unit":"usd"' in encoded
    assert result.evidence_ids == sorted(set(result.evidence_ids))


def pit_cutoff_and_unknown(tmp: Path) -> None:
    source = inspect.getsource(
        __import__("tradehub_research.screening", fromlist=["_load_facts"])._load_facts
    )
    assert "public_available_time <= ?" in source
    assert "source_reported','derived_from_index" in source


def restatement_replay(tmp: Path) -> None:
    source = inspect.getsource(
        __import__("tradehub_research.screening", fromlist=["_load_facts"])._load_facts
    )
    assert "supersedes_evidence_id" in source and "visible_chain" in source


def quarter_comparability(tmp: Path) -> None:
    source = inspect.getsource(inflection)
    assert "noncomparable_periods" in source and "duration" in source and "dimensions" in source


def source_completeness(tmp: Path) -> None:
    assert not informed_activity.evaluate(_context(), "S").sufficient_data
    assert event.evaluate(_context(identity_feed_complete=True), "S").sufficient_data


def price_pat_adjustment(tmp: Path) -> None:
    common = __import__("tradehub_research.hunters.common", fromlist=["eligible_bars"])
    source = inspect.getsource(common)
    assert "20, 15" in source and "public_available_time" in source
    assert "adjClose" not in inspect.getsource(momentum.evaluate)


def idempotent_recovery(tmp: Path) -> None:
    store = inspect.getsource(
        __import__("tradehub_research.screen_store", fromlist=["ScreenStore"]).ScreenStore
    )
    assert "stored screen result differs from deterministic retry" in store
    assert "funnel requires a COMPLETE pipeline run" in store


def candidate_merge_and_overflow(tmp: Path) -> None:
    universe = [f"S{i:02}" for i in range(51)]
    rows = [_row(s, "event", passed=True) for s in universe]
    candidates, flags = run_funnel(
        run_id="r",
        logical_material="m",
        config=FunnelConfig(),
        universe=universe,
        holdings={"S00"},
        results=rows,
        sectors={},
        cluster_lookup={},
    )
    assert len(candidates) == 51 and flags == ["budget_overflow_mandatory"]
    assert set(candidates[0].inclusion_reasons) == {"event_pass", "holding"}


def sector_round_robin_budget(tmp: Path) -> None:
    signals = [f"S{i:03}" for i in range(100)]
    controls = [f"C{i}" for i in range(10)]
    universe = signals + controls
    rows = [_row(s, "valuation", passed=True) for s in signals]
    for sid in controls:
        rows.extend(
            _row(sid, family, passed=False)
            for family in ("valuation", "inflection", "quality", "informed_activity", "event")
        )
    sectors = {sid: ("A" if i % 2 == 0 else "B") for i, sid in enumerate(signals)}
    a, _ = run_funnel(
        run_id="r",
        logical_material="m",
        config=FunnelConfig(),
        universe=universe,
        holdings=set(),
        results=rows,
        sectors=sectors,
        cluster_lookup={},
    )
    b, _ = run_funnel(
        run_id="r",
        logical_material="m",
        config=FunnelConfig(),
        universe=list(reversed(universe)),
        holdings=set(),
        results=list(reversed(rows)),
        sectors=sectors,
        cluster_lookup={},
    )
    assert len(a) == 50 and sum(x.is_control for x in a) == 5
    assert [(x.security_id, x.ordinal) for x in a] == [(x.security_id, x.ordinal) for x in b]
    assert [sectors[x.security_id] for x in a[:4]] == ["A", "B", "A", "B"]


def control_sample(tmp: Path) -> None:
    assert control_key("material", "A") == control_key("material", "A")
    assert control_key("material", "A") != control_key("material", "B")


def cluster_counting(tmp: Path) -> None:
    rows = [
        _row("S", "valuation", passed=True, evidence=("e1",)),
        _row("S", "quality", passed=True, evidence=("e2",)),
    ]
    candidates, _ = run_funnel(
        run_id="r",
        logical_material="m",
        config=FunnelConfig(control_count=0),
        universe=["S"],
        holdings=set(),
        results=rows,
        sectors={},
        cluster_lookup={"e1": {"c"}, "e2": {"c"}},
    )
    assert candidates[0].rank_telemetry["distinct_supporting_cluster_count"] == 1


def isolation_and_complexity(tmp: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    for path in [root / "hunters", root / "funnel.py", root / "screening.py"]:
        files = path.glob("*.py") if path.is_dir() else [path]
        for file in files:
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = (
                        [x.name for x in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    assert not any(n == "tradehub" or n.startswith("tradehub.") for n in names)
            lowered = file.read_text().lower()
            assert not any(
                token in lowered
                for token in ("requests.", "urllib", "preview_order", "submit_order")
            )


ASSERTIONS = [
    (name, globals()[name])
    for name in (
        "contract_all_screens",
        "spec_hash_and_collision",
        "population_tristate",
        "raw_feature_lineage",
        "pit_cutoff_and_unknown",
        "restatement_replay",
        "quarter_comparability",
        "source_completeness",
        "price_pat_adjustment",
        "idempotent_recovery",
        "candidate_merge_and_overflow",
        "sector_round_robin_budget",
        "control_sample",
        "cluster_counting",
        "isolation_and_complexity",
    )
]

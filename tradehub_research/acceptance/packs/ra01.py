"""RA-01: offline qualification for Phase-1 Hunters and candidate funnel."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tradehub_research.adapters.base import FetchResult
from tradehub_research.adapters.tiingo import TiingoEodAdapter
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.funnel import FunnelConfig, FunnelResultRow, control_key, run_funnel
from tradehub_research.hunters import (
    event,
    inflection,
    informed_activity,
    valuation,
)
from tradehub_research.hunters.common import adjusted_close_series
from tradehub_research.screen_store import ScreenStore
from tradehub_research.screening import (
    ScreeningConfig,
    _load_facts,
    _load_form4_coverage,
    _load_identity_feed_state,
    run_screening,
)
from tradehub_research.screens import (
    ScreenContext,
    ScreenResult,
    canonical_json,
    registered_screens,
)


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
    payloads = (strong, weak, missing)
    assert [(x.sufficient_data, x.passed) for x in payloads] == [
        (True, True),
        (True, False),
        (False, False),
    ]
    database = ResearchDB(tmp / "tristate.db")
    _seed_db(database)
    with database.connect() as db:
        for sid in ("strong", "weak", "missing"):
            db.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, sid, "NYSE", sid, None, None, "SUPPORTED", "2020-01-01", None),
            )
    store = ScreenStore(database)
    store.save_screen_definition(event.SCREEN_SPEC)
    run_id = store.begin_run(
        as_of="2025-04-01",
        universe_hash="u",
        screen_manifest=[{"config_hash": event.SCREEN_SPEC.config_hash, "expected_count": 3}],
        funnel_config={},
        input_view_hash="v",
        expected_security_count=3,
    )
    rows = [
        ScreenResult.create(
            run_id=run_id,
            security_id=sid,
            config_hash=event.SCREEN_SPEC.config_hash,
            raw_features=payload.raw_features,
            evidence_ids=payload.evidence_ids,
            reason_codes=payload.reason_codes,
            sufficient_data=payload.sufficient_data,
            passed=payload.passed,
            confidence=payload.confidence,
            data_quality=payload.data_quality,
        )
        for sid, payload in zip(("strong", "weak", "missing"), payloads, strict=True)
    ]
    store.persist_screen_population(run_id, event.SCREEN_SPEC.config_hash, rows)
    with database.connect(read_only=True) as db:
        persisted = db.execute(
            "SELECT sufficient_data,passed FROM screen_result "
            "ORDER BY CASE security_id WHEN 'strong' THEN 1 WHEN 'weak' THEN 2 ELSE 3 END"
        ).fetchall()
    assert [tuple(row) for row in persisted] == [(1, 1), (1, 0), (0, 0)]


def raw_feature_lineage(tmp: Path) -> None:
    result = valuation.evaluate(_context(), "S")
    encoded = canonical_json(result.raw_features)
    assert '"value":null' in encoded and '"unit":"usd"' in encoded
    assert result.evidence_ids == sorted(set(result.evidence_ids))


def pit_cutoff_and_unknown(tmp: Path) -> None:
    db = ResearchDB(tmp / "pit-load.db")
    store = _seed_db(db)
    for kind, pat, provenance in (
        ("form4_index_coverage", "2025-03-01", "derived_from_index"),
        ("form4_index_coverage", "2025-05-01", "derived_from_index"),
        ("identity_feed_marker", "2025-05-01", "source_reported"),
        ("identity_feed_marker", None, "unknown"),
    ):
        store.insert(
            security_id="S",
            source_id="src",
            structured_fields={"record_type": kind, "index_date": pat and pat[:10]},
            extraction_confidence=1,
            event_time="2025-01-01",
            public_available_time=pat,
            pat_provenance=provenance,
            ingested_time="2025-06-01",
        )
    with db.connect(read_only=True) as conn:
        assert _load_form4_coverage(conn, "2025-04-01", ["S"]) == {"S": frozenset({"2025-03-01"})}
        assert _load_identity_feed_state(conn, "2025-04-01", ["S"]) == {"S": False}


def restatement_replay(tmp: Path) -> None:
    db = ResearchDB(tmp / "restatement.db")
    store = _seed_db(db)
    original = store.insert(
        security_id="S",
        source_id="src",
        structured_fields={"record_type": "xbrl_fact", "metric": "revenue", "value": 1},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-05",
        pat_provenance="source_reported",
        ingested_time="2025-02-01",
    )
    correction = store.insert(
        security_id="S",
        source_id="src",
        structured_fields={"record_type": "xbrl_fact", "metric": "revenue", "value": 2},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-08",
        pat_provenance="source_reported",
        ingested_time="2025-02-01",
        supersedes_evidence_id=original,
    )
    with db.connect(read_only=True) as conn:
        assert _load_facts(conn, "2025-01-06", ["S"])["S"][0]["evidence_id"] == original
        assert _load_facts(conn, "2025-01-09", ["S"])["S"][0]["evidence_id"] == correction


def quarter_comparability(tmp: Path) -> None:
    fact = {
        "period_start": "2025-01-01",
        "period_end": "2025-03-31",
        "value": 10,
        "concept": "Revenue",
        "unit": "USD",
        "dimensions": {},
        "public_available_time": "x",
    }
    assert inflection._standalone_quarters([fact], 10) == {"2025-03-31": fact}
    mismatch = {
        **fact,
        "period_end": "2025-08-31",
        "period_start": "2025-01-01",
        "fiscal_year": 2025,
        "accession": "a",
        "dimensions": {"segment": "x"},
    }
    base = {
        **fact,
        "period_end": "2025-05-31",
        "period_start": "2025-01-01",
        "fiscal_year": 2025,
        "accession": "a",
    }
    assert inflection._standalone_quarters([base, mismatch], 10) is None


def source_completeness(tmp: Path) -> None:
    assert not informed_activity.evaluate(_context(), "S").sufficient_data
    assert event.evaluate(_context(identity_feed_complete=True), "S").sufficient_data


def price_pat_adjustment(tmp: Path) -> None:
    fixture = Path(__file__).resolve().parents[3] / "tests/fixtures/tiingo_eod.json"
    raw = fixture.read_bytes()
    meta = FetchResult("fixture://tiingo", "2025-03-01T00:00:00Z", 200, {}, raw, fixture)
    rows = TiingoEodAdapter(
        token="x", license_confirmed=True, user_agent="ra", cache_dir=tmp
    ).parse(raw, meta, ticker="EXM")
    bars = [
        dict(r.structured_fields) for r in rows if r.structured_fields["record_type"] == "price_bar"
    ]
    bars.insert(0, {"record_type": "price_bar", "session_date": "2025-01-01", "close": 21.0})
    actions = [
        {**r.structured_fields, "action_type": r.structured_fields["record_type"]}
        for r in rows
        if r.structured_fields["record_type"] != "price_bar"
    ]
    adjusted = adjusted_close_series(bars, actions)
    assert adjusted != [(b["session_date"], b["close"]) for b in bars]
    assert all("adjClose" not in b for b in bars)


def idempotent_recovery(tmp: Path) -> None:
    db = ResearchDB(tmp / "recovery.db")
    _seed_db(db)
    from tradehub_research.screen_store import ScreenStore

    store = ScreenStore(db)
    spec = valuation.SCREEN_SPEC
    store.save_screen_definition(spec)
    run = store.begin_run(
        as_of="2025-04-01",
        universe_hash="u",
        screen_manifest=[{"config_hash": spec.config_hash, "expected_count": 0}],
        funnel_config={},
        input_view_hash="v",
        expected_security_count=0,
    )
    store.complete_run(run)
    store.complete_run(run)
    assert store.load_candidates(run) == []


def _seed_db(database: ResearchDB) -> EvidenceStore:
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("S", "S", "NYSE", "S", None, None, "SUPPORTED", "2020-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("src", "fixture", 1, "", "source_reported"),
        )
    return EvidenceStore(database)


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
    database = ResearchDB(tmp / "overflow.db")
    _seed_db(database)
    from tradehub_research.screen_store import ScreenStore

    store = ScreenStore(database)
    run_id = store.begin_run(
        as_of="2025-04-01",
        universe_hash="u",
        screen_manifest=[],
        funnel_config={},
        input_view_hash="v",
        expected_security_count=0,
    )
    store.complete_run(run_id)
    store.persist_run_flags(run_id, flags)
    with database.connect(read_only=True) as db:
        assert db.execute(
            "SELECT flags_json FROM pipeline_run WHERE run_id=?", (run_id,)
        ).fetchone()[0] == canonical_json(flags)


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
    database = ResearchDB(tmp / "isolated.db")
    _seed_db(database)
    with (
        patch("httpx.Client.request", side_effect=AssertionError("network called")),
        patch("subprocess.run", side_effect=AssertionError("external command called")),
    ):
        run_id = run_screening(
            "2025-04-01", None, ScreeningConfig(holdings=frozenset({"S"})), database=database
        )
    with database.connect(read_only=True) as db:
        assert (
            db.execute("SELECT status FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()[0]
            == "COMPLETE"
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

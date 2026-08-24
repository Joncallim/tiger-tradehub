from __future__ import annotations

from tradehub_research.acceptance.runner import PACK_REGISTRY
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.funnel import FunnelConfig
from tradehub_research.screen_store import ScreenStore
from tradehub_research.screening import ScreeningConfig, _load_identity_feed_state, run_screening
from tradehub_research.snapshot import create_snapshot, open_snapshot_read_only
from tradehub_research.universe import UniverseMembershipStore


def test_screening_config_defaults() -> None:
    config = ScreeningConfig.from_dict({})
    assert config.funnel == FunnelConfig()
    assert config.universe_coverage == ("SUPPORTED",)
    assert config.holdings == frozenset()


def test_funnel_config_hash_is_canonical_and_sensitive() -> None:
    assert FunnelConfig().config_hash == FunnelConfig().config_hash
    assert FunnelConfig().config_hash != FunnelConfig(budget=49).config_hash


def test_ra01_is_explicitly_registered_with_fifteen_assertions() -> None:
    assert len(PACK_REGISTRY["RA-01"]) == 15


def test_run_screening_end_to_end_persists_holding_candidate(tmp_path) -> None:
    database = ResearchDB(tmp_path / "screen.db")
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("S", "S", "NYSE", "S", None, None, "SUPPORTED", "2020-01-01", None),
        )
    UniverseMembershipStore(database).insert(
        security_id="S",
        price=10,
        market_cap=1e9,
        avg_dollar_volume=1e7,
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
        valid_from="2020-01-01",
        knowledge_time="2025-01-01",
        pat_provenance="derived_from_index",
    )
    run_id = run_screening(
        "2025-04-01T00:00:00Z", None, ScreeningConfig(holdings=frozenset({"S"})), database=database
    )
    with database.connect(read_only=True) as db:
        candidate = db.execute(
            "SELECT security_id FROM candidate WHERE run_id=?", (run_id,)
        ).fetchone()
        flags = db.execute(
            "SELECT flags_json FROM pipeline_run WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    assert candidate[0] == "S"
    assert flags == "[]"
    assert (
        run_screening(
            "2025-04-01T00:00:00Z",
            None,
            ScreeningConfig(holdings=frozenset({"S"})),
            database=database,
        )
        == run_id
    )


def test_holdings_change_run_identity(tmp_path) -> None:
    database = ResearchDB(tmp_path / "identity.db")
    database.init()
    from tradehub_research.screen_store import ScreenStore

    store = ScreenStore(database)
    args = dict(
        as_of="2025-04-01",
        universe_hash="u",
        screen_manifest=[],
        input_view_hash="v",
        expected_security_count=0,
    )
    a = store.begin_run(**args, funnel_config={"holdings": ["A"]})
    b = store.begin_run(**args, funnel_config={"holdings": ["B"]})
    assert a != b


def test_identity_feed_completeness_is_loaded_per_security(tmp_path) -> None:
    database = ResearchDB(tmp_path / "coverage.db")
    database.init()
    with database.connect() as db:
        for sid in ("A", "B"):
            db.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, sid, "NYSE", sid, None, None, "SUPPORTED", "2020-01-01", None),
            )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)", ("src", "x", 1, "", "source_reported")
        )
    EvidenceStore(database).insert(
        security_id="A",
        source_id="src",
        structured_fields={"record_type": "identity_feed_marker"},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-02",
        pat_provenance="source_reported",
        ingested_time="2025-01-03",
    )
    with database.connect(read_only=True) as db:
        assert _load_identity_feed_state(db, "2025-04-01", ["A", "B"]) == {"A": True, "B": False}


def test_cluster_lookup_is_as_of_and_uses_snapshot_connection(tmp_path) -> None:
    database = ResearchDB(tmp_path / "clusters.db")
    database.init()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("S", "S", "NYSE", "S", None, None, "SUPPORTED", "2020-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)", ("src", "x", 1, "", "source_reported")
        )
    evidence_id = EvidenceStore(database).insert(
        security_id="S",
        source_id="src",
        structured_fields={"record_type": "xbrl_fact"},
        extraction_confidence=1,
        event_time="2025-01-01",
        public_available_time="2025-01-02",
        pat_provenance="source_reported",
        ingested_time="2025-01-03",
    )
    with database.connect() as db:
        db.execute(
            "INSERT INTO evidence_cluster VALUES (?,?,?)", ("past", "past", "2025-01-03T00:00:00Z")
        )
        db.execute(
            "INSERT INTO evidence_cluster VALUES (?,?,?)",
            ("future", "future", "2025-05-01T00:00:00Z"),
        )
        db.execute("INSERT INTO evidence_cluster_member VALUES (?,?)", (evidence_id, "past"))
        db.execute("INSERT INTO evidence_cluster_member VALUES (?,?)", (evidence_id, "future"))
    store = ScreenStore(database)
    assert store.cluster_ids_by_evidence("2025-04-01") == {evidence_id: {"past"}}
    snapshot_path = tmp_path / "snapshot.db"
    create_snapshot(database, snapshot_path)
    with database.connect() as db:
        db.execute(
            "INSERT INTO evidence_cluster VALUES (?,?,?)",
            ("live-only", "live", "2025-01-03T00:00:00Z"),
        )
        db.execute("INSERT INTO evidence_cluster_member VALUES (?,?)", (evidence_id, "live-only"))
    snapshot_db = open_snapshot_read_only(snapshot_path).connection()
    try:
        assert store.cluster_ids_by_evidence("2025-04-01", connection=snapshot_db) == {
            evidence_id: {"past"}
        }
    finally:
        snapshot_db.close()

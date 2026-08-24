from __future__ import annotations

from tradehub_research.acceptance.runner import PACK_REGISTRY
from tradehub_research.db import ResearchDB
from tradehub_research.funnel import FunnelConfig
from tradehub_research.screening import ScreeningConfig, run_screening
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

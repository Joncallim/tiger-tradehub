"""Portfolio snapshot and signal input typing, identity, and store."""

from __future__ import annotations

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.snapshot import (
    SnapshotStore,
    build_signal_input,
    build_snapshot,
)


def _holding(**overrides):
    row = {
        "security_id": "sec1",
        "quantity_microunits": 1_000_000,
        "sellable_quantity_microunits": 1_000_000,
        "market_value_microusd": 5_000_000_000,
        "sector": "Tech",
    }
    row.update(overrides)
    return row


def _market(**overrides):
    row = {
        "security_id": "sec1",
        "mark_price_microusd": 50_000_000,
        "price_as_of": "2025-06-01T00:00:00Z",
        "avg_dollar_volume_microusd": 1_000_000_000_000,
        "liquidity_as_of": "2025-06-01T00:00:00Z",
        "evidence_ids": ["e1"],
    }
    row.update(overrides)
    return row


def test_snapshot_identity_is_deterministic_and_order_independent():
    a = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=5_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[_holding()],
        market_inputs=[_market()],
    )
    b = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=5_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[_holding(security_id="sec1")],
        market_inputs=[_market(security_id="sec1")],
    )
    assert a.snapshot_id == b.snapshot_id
    assert a.input_hash == b.input_hash


def test_snapshot_identity_changes_with_content():
    a = build_snapshot(
        "2025-06-01T00:00:00Z", cash_microusd=10_000_000_000, nav_microusd=10_000_000_000
    )
    b = build_snapshot(
        "2025-06-01T00:00:00Z", cash_microusd=11_000_000_000, nav_microusd=11_000_000_000
    )
    assert a.snapshot_id != b.snapshot_id


def test_nav_child_sum_mismatch_rejected():
    with pytest.raises(ValueError, match="NAV mismatch"):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=5_000_000_000,
            nav_microusd=12_000_000_000,
            holdings=[_holding()],
        )


def test_nav_known_requires_all_holding_values_known():
    with pytest.raises(ValueError, match="every holding market value KNOWN"):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=5_000_000_000,
            nav_microusd=10_000_000_000,
            holdings=[_holding(market_value_microusd=None, valuation_status="UNKNOWN")],
        )


def test_empty_known_differs_from_unknown():
    empty_known = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=10_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[],
    )
    unknown = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=None,
        cash_status="UNKNOWN",
        nav_microusd=None,
        valuation_status="UNKNOWN",
        holdings_status="UNKNOWN",
        holdings=[],
    )
    assert empty_known.snapshot_id != unknown.snapshot_id
    assert empty_known.cash_status.value == "KNOWN"
    assert unknown.cash_status.value == "UNKNOWN"


def test_status_orthogonality_enforced():
    with pytest.raises(ValueError):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=None,
            cash_status="KNOWN",
            nav_microusd=10_000_000_000,
        )
    with pytest.raises(ValueError):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=1_000_000,
            cash_status="UNKNOWN",
            nav_microusd=10_000_000_000,
        )


def test_negative_cash_and_nonpositive_mark_rejected():
    with pytest.raises(ValueError):
        build_snapshot("2025-06-01T00:00:00Z", cash_microusd=-1, nav_microusd=10_000_000_000)
    with pytest.raises(ValueError):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=10_000_000_000,
            nav_microusd=10_000_000_000,
            market_inputs=[_market(mark_price_microusd=0)],
        )


def test_sellable_bounds_and_duplicates():
    with pytest.raises(ValueError):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=5_000_000_000,
            nav_microusd=10_000_000_000,
            holdings=[_holding(sellable_quantity_microunits=2_000_000)],
        )
    with pytest.raises(ValueError, match="duplicate holding"):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=5_000_000_000,
            nav_microusd=10_000_000_000,
            holdings=[_holding(), _holding()],
        )


def test_signal_input_identity_and_validation():
    a = build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=500000)
    b = build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=500000)
    assert a.signal_input_id == b.signal_input_id
    c = build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=500001)
    assert a.signal_input_id != c.signal_input_id
    with pytest.raises(ValueError):
        build_signal_input(
            "sec1",
            "2025-06-01T00:00:00Z",
            remaining_opportunity_ppm=None,
            opportunity_status="KNOWN",
        )
    with pytest.raises(ValueError):
        build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=2_000_000)
    unknown = build_signal_input("sec1", "2025-06-01T00:00:00Z")
    assert unknown.opportunity_status.value == "UNKNOWN"
    assert unknown.remaining_opportunity_ppm is None


def test_store_equality_checked_idempotent(tmp_path):
    database = ResearchDB(tmp_path / "snapshot.db")
    database.migrate()
    store = SnapshotStore(database)
    snapshot = build_snapshot(
        "2025-06-01T00:00:00Z", cash_microusd=10_000_000_000, nav_microusd=10_000_000_000
    )
    store.save_snapshot(snapshot)
    store.save_snapshot(snapshot)  # idempotent
    signal = build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=500000)
    store.save_signal_input(signal)
    with database.connect(read_only=True) as db:
        assert db.execute("SELECT count(*) FROM portfolio_snapshot").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM portfolio_signal_input").fetchone()[0] == 1


def test_sector_total_ppm():
    snapshot = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=5_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[
            _holding(security_id="sec1", market_value_microusd=3_000_000_000, sector="Tech"),
            _holding(security_id="sec2", market_value_microusd=2_000_000_000, sector="Tech"),
        ],
    )
    assert snapshot.sector_total_ppm("Tech") == 500000
    assert snapshot.sector_total_ppm("Health") == 0

"""Risk engine: UNKNOWN composition, clips, correlation/volatility/liquidity."""

from __future__ import annotations

import pytest

from tests.portfolio_test_helpers import seed_price_bars, seed_security
from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.fixtures import fixture_policy
from tradehub_research.portfolio.risk import RiskEngine, RiskInputs
from tradehub_research.portfolio.snapshot import build_snapshot
from tradehub_research.portfolio.types import Action, State


def _policy():
    return fixture_policy()


def _risk_inputs(**overrides):
    base = dict(
        security_id="sec1",
        sector="Tech",
        sector_coverage_status="SUPPORTED",
        current_state=State.WATCH,
        position_present=False,
        trusted_quantity_microunits=None,
        sellable_quantity_microunits=None,
        mark_price_microusd=50_000_000,
        price_status="KNOWN",
        price_as_of="2025-06-01T00:00:00Z",
        adv_microusd=51_450_000_000_000,  # matches ledger ADV (51.45 avg close x 1M vol)
        liquidity_status="KNOWN",
        liquidity_as_of="2025-06-01T00:00:00Z",
        nav_microusd=10_000_000_000,
        cash_microusd=5_000_000_000,
        current_weight_ppm=0,
        direction=Action.BUY,
    )
    base.update(overrides)
    return RiskInputs(**base)


def _snapshot(holdings=None, market=None, nav=10_000_000_000, cash=10_000_000_000):
    return build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=cash,
        nav_microusd=nav,
        holdings=holdings or [],
        market_inputs=market or [],
    )


@pytest.fixture()
def runtime(tmp_path):
    database = ResearchDB(tmp_path / "risk.db")
    database.migrate()
    with database.connect() as db:
        seed_security(db, "sec1", sector="Tech")
        closes = [50 + (i % 7) * 0.5 for i in range(40)]
        seed_price_bars(db, "sec1", closes=closes)
    return database


def test_buy_blocks_on_unknown_cash(runtime):
    engine = RiskEngine(runtime, _policy(), _snapshot())
    result = engine.evaluate(
        _risk_inputs(
            cash_microusd=None,
            price_status="UNKNOWN",
            price_as_of=None,
            adv_microusd=None,
            liquidity_status="UNKNOWN",
            nav_microusd=None,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "BLOCKED"
    assert "cash_unknown" in result.reasons
    assert "nav_unknown" in result.reasons
    assert "liquidity_unknown" in result.reasons


def test_buy_blocks_on_unknown_mark(runtime):
    engine = RiskEngine(runtime, _policy(), _snapshot())
    result = engine.evaluate(
        _risk_inputs(mark_price_microusd=None, price_status="UNKNOWN", price_as_of=None),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "BLOCKED"
    assert "mark_unknown" in result.reasons


def test_buy_blocks_on_stale_mark(runtime):
    engine = RiskEngine(runtime, _policy(), _snapshot())
    result = engine.evaluate(
        _risk_inputs(price_status="STALE", price_as_of="2025-05-20T00:00:00Z"),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "BLOCKED"
    assert "mark_stale" in result.reasons


def test_buy_blocks_on_unknown_volatility(tmp_path):
    database = ResearchDB(tmp_path / "novol.db")
    database.migrate()
    with database.connect() as db:
        seed_security(db, "sec1", sector="Tech")  # no price bars at all
    engine = RiskEngine(database, _policy(), _snapshot())
    result = engine.evaluate(_risk_inputs(), "2025-06-01T00:00:00Z")
    assert result.status == "BLOCKED"
    assert "volatility_unknown" in result.reasons


def test_buy_pass_with_clips(runtime):
    engine = RiskEngine(runtime, _policy(), _snapshot())
    result = engine.evaluate(_risk_inputs(), "2025-06-01T00:00:00Z")
    assert result.status == "PASS"
    assert result.clips["position"] == 100000
    assert result.clips["sector"] == 250000
    assert result.clips["volatility"] == 100000
    assert result.clips["liquidity"] > 0
    assert result.measures["annualized_vol_ppm"] is not None


def test_sell_requires_quantity_and_sellable(runtime):
    engine = RiskEngine(runtime, _policy(), _snapshot())
    result = engine.evaluate(
        _risk_inputs(
            current_state=State.HOLD,
            position_present=True,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            direction=Action.SELL,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "BLOCKED"
    assert "quantity_unknown" in result.reasons
    assert "sellable_unknown" in result.reasons


def test_sell_is_limited_when_volatility_unknown(tmp_path):
    database = ResearchDB(tmp_path / "novol.db")
    database.migrate()
    with database.connect() as db:
        seed_security(db, "sec1", sector="Tech")  # no price bars at all
    engine = RiskEngine(database, _policy(), _snapshot())
    result = engine.evaluate(
        _risk_inputs(
            current_state=State.HOLD,
            position_present=True,
            trusted_quantity_microunits=1_000_000,
            sellable_quantity_microunits=1_000_000,
            direction=Action.SELL,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "LIMITED"  # exposure decreases: never relabelled PASS
    assert "volatility_unknown" in result.reasons


def test_factor_and_drawdown_seams_honest(runtime):
    from tradehub_research.portfolio.policy import build_policy
    from tradehub_research.portfolio.types import PolicyStatus

    spec = _policy().as_dict()
    spec["risk"]["factor_required"] = True
    policy = build_policy("factor-required", PolicyStatus.FIXTURE, spec)
    engine = RiskEngine(runtime, policy, _snapshot())
    result = engine.evaluate(_risk_inputs(), "2025-06-01T00:00:00Z")
    assert result.status == "BLOCKED"
    assert "factor_seam_required_unavailable" in result.reasons


def test_concentration_cap_clips_target(runtime):
    # holding weight 60% > max_position 10% -> risk_reduction context in engine;
    # here we verify the sector clip math via a snapshot with a big sector.
    with runtime.connect() as db:
        seed_security(db, "sec2", sector="Tech")
        seed_price_bars(db, "sec2", closes=[50 + (i % 7) * 0.5 for i in range(40)])
    snapshot = _snapshot(
        holdings=[
            {
                "security_id": "sec2",
                "quantity_microunits": 1_000_000,
                "sellable_quantity_microunits": 1_000_000,
                "market_value_microusd": 4_000_000_000,
                "sector": "Tech",
            },
        ],
        cash=6_000_000_000,
    )
    engine = RiskEngine(runtime, _policy(), snapshot)
    result = engine.evaluate(_risk_inputs(current_weight_ppm=400000), "2025-06-01T00:00:00Z")
    assert result.status == "PASS"
    # sector clip: current + max(0, 250000 - 400000) = current
    assert result.clips["sector"] == 400000


def test_correlation_blocks_correlated_book(tmp_path):
    database = ResearchDB(tmp_path / "corr.db")
    database.migrate()
    with database.connect() as db:
        seed_security(db, "sec1", sector="Tech")
        seed_security(db, "sec2", sector="Tech")
        # perfectly correlated price paths (same shape, different scale)
        closes = [50 + i * 0.25 for i in range(60)]
        seed_price_bars(db, "sec1", closes=closes)
        seed_price_bars(db, "sec2", closes=[2 * c for c in closes])
    runtime = database
    snapshot = _snapshot(
        holdings=[
            {
                "security_id": "sec2",
                "quantity_microunits": 1_000_000,
                "sellable_quantity_microunits": 1_000_000,
                "market_value_microusd": 2_000_000_000,
                "sector": "Tech",
            },
        ],
        cash=8_000_000_000,
    )
    engine = RiskEngine(runtime, _policy(), snapshot)
    # ledger ADV for sec1 here: mean(60..64.75) x 1M vol
    result = engine.evaluate(_risk_inputs(adv_microusd=62_375_000_000_000), "2025-06-01T00:00:00Z")
    assert result.status == "PASS"
    correlated = result.measures.get("correlated_book_ppm", 0)
    assert correlated >= 200000  # sec2 20% weight is correlated with sec1
    assert result.clips["correlation"] == 0  # max_correlated_book 150000 already exceeded


def test_insufficient_overlap_correlation_is_unknown(tmp_path):
    database = ResearchDB(tmp_path / "overlap.db")
    database.migrate()
    with database.connect() as db:
        seed_security(db, "sec1", sector="Tech")
        seed_security(db, "sec2", sector="Tech")
        closes = [50 + i * 0.25 for i in range(60)]
        seed_price_bars(db, "sec1", closes=closes)
        # sec2 bars share no session dates with sec1 (non-overlapping windows)
        seed_price_bars(db, "sec2", closes=closes, start_date="2025-04-01")
    runtime = database
    snapshot = _snapshot(
        holdings=[
            {
                "security_id": "sec2",
                "quantity_microunits": 1_000_000,
                "sellable_quantity_microunits": 1_000_000,
                "market_value_microusd": 2_000_000_000,
                "sector": "Tech",
            },
        ],
        cash=8_000_000_000,
    )
    engine = RiskEngine(runtime, _policy(), snapshot)
    result = engine.evaluate(_risk_inputs(adv_microusd=62_375_000_000_000), "2025-06-01T00:00:00Z")
    # fail-closed: unassessable correlation on a material holding blocks the
    # increase — it is never silently converted to zero exposure
    assert result.status == "BLOCKED"
    assert "correlation_unassessable" in result.reasons
    assert result.measures.get("unassessable_holdings") == [
        {"security_id": "sec2", "weight_ppm": 200000, "reason": "correlation_unassessable"}
    ]


def test_average_dollar_volume_matches_expected():
    import pathlib
    import tempfile

    from tradehub_research.portfolio.prices import average_dollar_volume

    with tempfile.TemporaryDirectory() as d:
        db = ResearchDB(pathlib.Path(d) / "adv.db")
        db.migrate()
        with db.connect() as conn:
            seed_security(conn, "sec1")
            seed_price_bars(conn, "sec1", closes=[50.0] * 20, volumes=[1_000_000] * 20)
            adv = average_dollar_volume(conn, "sec1", "2025-06-01T00:00:00Z", 20, 15)
        assert adv == 50_000_000 * 1_000_000  # 50 * 1M shares in micro-USD

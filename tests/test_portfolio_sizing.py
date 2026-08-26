"""Sizing: deterministic band/multiplier math, cash/no-action paths, SELL bounds."""

from __future__ import annotations

from tradehub_research.portfolio.fixtures import fixture_policy
from tradehub_research.portfolio.sizing import size_buy, size_sell
from tradehub_research.portfolio.types import Action

PPM = 1_000_000


def test_band_discontinuity_proves_nonlinearity():
    # conviction 69 (band 2: 50000) vs conviction 70 (band 1: 80000) — a jump,
    # not a linear mapping.
    a = size_buy(
        fixture_policy(),
        conviction_ppm=690000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=5_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    b = size_buy(
        fixture_policy(),
        conviction_ppm=700000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=5_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert a.action == Action.BUY and b.action == Action.BUY
    assert a.target_weight_ppm == 50000
    assert b.target_weight_ppm == 80000
    assert b.target_weight_ppm != int(690000 / 700000 * 80000)


def test_buy_target_zero_is_no_action():
    result = size_buy(
        fixture_policy(),
        conviction_ppm=300000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=5_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action is None
    assert result.max_notional_microusd == 0


def test_buy_clip_reduces_target():
    # liquidity clip of 20000 caps target at 2%
    result = size_buy(
        fixture_policy(),
        conviction_ppm=900000,
        data_quality_ppm=900000,
        agreement_ppm=900000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 20000,
        },
        available_cash_microusd=5_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action == Action.BUY
    assert result.target_weight_ppm == 20000


def test_buy_cash_cap_limits_notional():
    # only $500 cash available: notional is capped at 500_000_000 micro-USD
    result = size_buy(
        fixture_policy(),
        conviction_ppm=900000,
        data_quality_ppm=900000,
        agreement_ppm=900000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=500_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action == Action.BUY
    assert result.max_notional_microusd <= 500_000_000


def test_buy_below_min_notional_is_no_action():
    result = size_buy(
        fixture_policy(),
        conviction_ppm=900000,
        data_quality_ppm=900000,
        agreement_ppm=900000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=100_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action is None


def test_sell_trim_reduces_target_and_bounds_quantity():
    result = size_sell(
        fixture_policy(),
        current_weight_ppm=80000,
        current_quantity_microunits=1_000_000,
        sellable_quantity_microunits=1_000_000,
        mark_price_microusd=50_000_000,
        nav_microusd=10_000_000_000,
        quantity_increment_microunits=1_000_000,
        full_exit=False,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action == Action.SELL
    assert result.target_weight_ppm == 40000  # trim_remaining_fraction 50%
    assert result.max_quantity_microunits <= 1_000_000
    assert result.completion_quantity_microunits >= 0


def test_sell_cannot_exceed_sellable():
    result = size_sell(
        fixture_policy(),
        current_weight_ppm=80000,
        current_quantity_microunits=1_000_000,
        sellable_quantity_microunits=400_000,
        mark_price_microusd=50_000_000,
        nav_microusd=10_000_000_000,
        quantity_increment_microunits=1_000_000,
        full_exit=True,
        min_action_notional_microusd=1_000_000,
    )
    # full exit with sellable < current degrades to TRIM, never oversells
    assert result.action == Action.SELL
    assert result.max_quantity_microunits <= 400_000
    assert result.completion_quantity_microunits >= 600_000


def test_sell_full_exit_target_zero():
    result = size_sell(
        fixture_policy(),
        current_weight_ppm=80000,
        current_quantity_microunits=1_000_000,
        sellable_quantity_microunits=1_000_000,
        mark_price_microusd=50_000_000,
        nav_microusd=10_000_000_000,
        quantity_increment_microunits=1_000_000,
        full_exit=True,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action == Action.SELL
    assert result.target_weight_ppm == 0
    assert result.completion_quantity_microunits == 0


def test_sell_zero_holdings_is_no_action():
    result = size_sell(
        fixture_policy(),
        current_weight_ppm=0,
        current_quantity_microunits=0,
        sellable_quantity_microunits=0,
        mark_price_microusd=50_000_000,
        nav_microusd=10_000_000_000,
        quantity_increment_microunits=1_000_000,
        full_exit=True,
        min_action_notional_microusd=1_000_000,
    )
    assert result.action is None


def test_no_fake_factor_exposure():
    # sizing never invents factor exposure; the seam is absent from the result
    result = size_buy(
        fixture_policy(),
        conviction_ppm=900000,
        data_quality_ppm=900000,
        agreement_ppm=900000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=5_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert "factor" not in result.detail

from __future__ import annotations

from tradehub_research.hunters import event, informed_activity, quality, valuation
from tradehub_research.screens import ScreenContext, registered_screens


def context(**updates) -> ScreenContext:
    values = {
        "facts": {},
        "price_bars": {},
        "form4": {},
        "identity_events": {},
        "market_caps": {},
        "universe": ["S"],
        "as_of": "2025-04-01T00:00:00Z",
        "sectors": {"S": "Technology"},
    }
    values.update(updates)
    return ScreenContext(**values)


def test_six_hunters_register_on_package_import() -> None:
    assert len(registered_screens()) == 6
    assert {spec.family for spec, _ in registered_screens()} == {
        "valuation",
        "inflection",
        "quality",
        "informed_activity",
        "event",
        "momentum_confirmation",
    }


def test_event_complete_zero_and_incomplete_are_distinct() -> None:
    missing = event.evaluate(context(identity_feed_complete=False), "S")
    negative = event.evaluate(context(identity_feed_complete=True), "S")
    assert (missing.sufficient_data, missing.passed) == (False, False)
    assert (negative.sufficient_data, negative.passed) == (True, False)


def test_event_feed_completeness_is_per_security() -> None:
    ctx = context(
        universe=["A", "B"],
        identity_feed_complete={"A": True, "B": False},
        sectors={"A": "Technology", "B": "Technology"},
    )
    assert event.evaluate(ctx, "A").reason_codes == ["no_identity_event"]
    uncovered = event.evaluate(ctx, "B")
    assert not uncovered.sufficient_data
    assert uncovered.reason_codes == ["incomplete_identity_feed"]


def test_form4_zero_requires_complete_window() -> None:
    result = informed_activity.evaluate(context(), "S")
    assert not result.sufficient_data
    assert result.reason_codes == ["missing_form4_coverage"]


def test_unsupported_sector_rules() -> None:
    bank = context(sectors={"S": "Banks"})
    reit = context(sectors={"S": "REITs"})
    assert valuation.evaluate(bank, "S").reason_codes == ["unsupported_sector"]
    assert quality.evaluate(reit, "S").reason_codes == ["unsupported_sector"]


def test_thresholds_are_versioned_in_specs() -> None:
    assert valuation.SCREEN_SPEC.parameters["min_earnings_yield"] == 0.05
    assert "min_earnings_yield" not in valuation.evaluate.__dict__

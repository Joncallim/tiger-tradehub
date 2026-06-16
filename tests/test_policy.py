import pytest

from tradehub.config import Settings
from tradehub.models import OrderIntent
from tradehub.policy import PolicyError, validate_order_intent


def settings(**overrides):
    base = {"TRADEHUB_API_TOKEN": "test-token"}
    base.update(overrides)
    return Settings(**base)


def test_limit_order_within_policy_is_accepted():
    intent = OrderIntent(symbol="aapl", side="BUY", quantity=1, limit_price=150)

    warnings = validate_order_intent(intent, settings())

    assert intent.symbol == "AAPL"
    assert any("Dry-run" in warning for warning in warnings)


def test_symbol_allowlist_blocks_unknown_symbol():
    intent = OrderIntent(symbol="TSLA", side="BUY", quantity=1, limit_price=150)

    with pytest.raises(PolicyError):
        validate_order_intent(intent, settings(TRADEHUB_SYMBOL_ALLOWLIST="AAPL,MSFT"))


def test_market_order_blocked_by_default():
    intent = OrderIntent(symbol="AAPL", side="BUY", quantity=1, order_type="MARKET")

    with pytest.raises(PolicyError):
        validate_order_intent(intent, settings())


def test_notional_limit_blocks_large_order():
    intent = OrderIntent(symbol="AAPL", side="BUY", quantity=10, limit_price=150)

    with pytest.raises(PolicyError):
        validate_order_intent(intent, settings(TRADEHUB_MAX_NOTIONAL_USD=100))


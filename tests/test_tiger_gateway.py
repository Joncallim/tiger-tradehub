from tradehub.config import Settings
from tradehub.models import OrderIntent
from tradehub.tiger_gateway import TigerGateway

STRONG_TOKEN = "test-token-with-enough-length"


class FakeTradeClient:
    def __init__(self):
        self.cancel_kwargs = None
        self.place_response = {"order_id": "broker-order-123"}

    def cancel_order(self, **kwargs):
        self.cancel_kwargs = kwargs
        return {"cancelled": True}

    def place_order(self, order):
        return self.place_response


class FakeGateway(TigerGateway):
    def _build_order(self, intent):
        return type("Order", (), {"id": "local-order-456"})()


def settings():
    return Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TIGEROPEN_TIGER_ID="tiger-id",
        TIGEROPEN_ACCOUNT="account-123",
        TIGEROPEN_PRIVATE_KEY="private-key",
    )


def test_cancel_order_uses_explicit_account_and_order_id_keywords():
    gateway = TigerGateway(settings())
    client = FakeTradeClient()
    gateway._trade_client = client

    response = gateway.cancel_order("order-123")

    assert response == {"cancelled": True}
    assert client.cancel_kwargs == {"account": "account-123", "order_id": "order-123"}


def test_place_order_prefers_broker_response_order_id():
    gateway = FakeGateway(settings())
    gateway._trade_client = FakeTradeClient()

    order_id, response = gateway.place_order(
        OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)
    )

    assert order_id == "broker-order-123"
    assert response == {"order_id": "broker-order-123"}


def test_place_order_accepts_scalar_broker_order_id():
    gateway = FakeGateway(settings())
    client = FakeTradeClient()
    client.place_response = 12345
    gateway._trade_client = client

    order_id, response = gateway.place_order(
        OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)
    )

    assert order_id == "12345"
    assert response == {"value": "12345"}

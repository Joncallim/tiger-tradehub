import json

from tradehub.config import Settings
from tradehub.models import OrderIntent
from tradehub.tiger_gateway import (
    TigerGateway,
    classify_preview,
    normalize,
    normalize_collection,
)

STRONG_TOKEN = "test-token-with-enough-length"


class FakeContract:
    """Mimics tigeropen.trade.domain.contract.Contract: has to_dict() but is not a dict."""

    def __init__(self):
        self.symbol = "AAPL"
        self.currency = "USD"
        self.sec_type = "STK"

    def to_dict(self):
        return {"symbol": self.symbol, "currency": self.currency, "sec_type": self.sec_type}


class FakeOrder:
    """Mimics tigeropen.trade.domain.order.Order.to_dict(), which embeds a raw Contract object."""

    def __init__(self):
        self.contract = FakeContract()
        self.order_id = "broker-order-123"
        self.status = "SUBMITTED"

    def to_dict(self):
        return {"contract": self.contract, "order_id": self.order_id, "status": self.status}


class FakePosition:
    """Mimics tigeropen Position.to_dict(), which embeds a raw Contract object under 'contract'."""

    def __init__(self):
        self.contract = FakeContract()
        self.quantity = 10

    def to_dict(self):
        return {"contract": self.contract, "quantity": self.quantity}


class FakeTradeClient:
    def __init__(self):
        self.cancel_kwargs = None
        self.create_kwargs = None
        self.place_response = {"order_id": "broker-order-123"}
        self.placed_order = None
        self.get_order_kwargs = None

    def cancel_order(self, **kwargs):
        self.cancel_kwargs = kwargs
        return {"cancelled": True}

    def create_order(self, **kwargs):
        self.create_kwargs = kwargs
        order = type("Order", (), {})()
        order.contract = kwargs["contract"]
        order.order_type = kwargs["order_type"]
        order.action = kwargs["action"]
        order.quantity = kwargs["quantity"]
        order.limit_price = kwargs.get("limit_price")
        order.order_id = kwargs.get("order_id") or "reserved-999"
        return order

    def place_order(self, order):
        self.placed_order = order
        return self.place_response

    def get_order(self, **kwargs):
        self.get_order_kwargs = kwargs
        if str(kwargs["order_id"]) == "reserved-999":
            return {"id": "global-999", "order_id": "reserved-999"}
        if str(kwargs["order_id"]) == "0":
            return {"id": "global-missing", "order_id": "0"}
        return None


class FakeGateway(TigerGateway):
    def _build_order(self, intent):
        order_type = (
            intent.order_type.value if hasattr(intent.order_type, "value") else intent.order_type
        )
        return type(
            "Order",
            (),
            {
                "id": "local-order-456",
                "contract": FakeContract(),
                "action": intent.side.value,
                "order_type": order_type,
                "quantity": intent.quantity,
                "limit_price": intent.limit_price,
            },
        )()


def settings():
    return Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TIGEROPEN_TIGER_ID="tiger-id",
        TIGEROPEN_ACCOUNT="account-123",
        TIGEROPEN_PRIVATE_KEY="private-key",
    )


def test_classify_preview_accepts_documented_success_shape():
    assert (
        classify_preview(
            {
                "init_margin_before": 0,
                "init_margin": 0,
                "maint_margin_before": 0,
                "maint_margin": 0,
                "margin_currency": "USD",
                "equity_with_loan_before": 100,
                "equity_with_loan": 100,
                "min_commission": 0,
                "max_commission": 1,
                "commission_currency": "USD",
            }
        )
        == "accepted"
    )


def test_classify_preview_rejects_warning_only_shape():
    assert classify_preview({"warning_text": "insufficient buying power"}) == "rejected"


def test_classify_preview_rejects_nonempty_message_even_when_is_pass_true():
    assert (
        classify_preview(
            {
                "is_pass": True,
                "message": "cannot submit this order",
                "init_margin": 0,
                "maint_margin": 0,
                "equity_with_loan": 100,
                "margin_currency": "USD",
                "commission_currency": "USD",
                "commission": 0,
            }
        )
        == "rejected"
    )


def test_classify_preview_rejects_is_pass_false():
    assert classify_preview({"is_pass": False, "message": "rejected"}) == "rejected"


def test_classify_preview_fails_closed_for_unknown_shape():
    assert classify_preview({"status": "skipped"}) == "unknown"


def test_classify_preview_fails_closed_for_none():
    assert classify_preview(None) == "unknown"


def test_cancel_order_passes_global_order_id_via_id_param():
    """tigeropen's cancel_order has two distinct params: `id` (global order id, what
    place_order returns and what TradeHub records) and `order_id` (account-specific
    id). Passing the global id into `order_id` is rejected by Tiger with
    ApiException code=1010 'biz param error'; only `id` works."""
    gateway = TigerGateway(settings())
    client = FakeTradeClient()
    gateway._trade_client = client

    response = gateway.cancel_order("44386595912828928")

    assert response == {"cancelled": True}
    assert client.cancel_kwargs == {"account": "account-123", "id": 44386595912828928}


def test_cancel_order_casts_order_id_to_int_for_sdk():
    gateway = TigerGateway(settings())
    client = FakeTradeClient()
    gateway._trade_client = client

    gateway.cancel_order("27")

    assert client.cancel_kwargs == {"account": "account-123", "id": 27}
    assert isinstance(client.cancel_kwargs["id"], int)


def test_place_order_prefers_broker_response_order_id():
    gateway = FakeGateway(settings())
    gateway._trade_client = FakeTradeClient()
    order = gateway.create_order(
        OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)
    )

    order_id, response = gateway.place_order(order)

    assert order_id == "broker-order-123"
    assert response == {"order_id": "broker-order-123"}


def test_place_order_accepts_scalar_broker_order_id():
    gateway = FakeGateway(settings())
    client = FakeTradeClient()
    client.place_response = 12345
    gateway._trade_client = client
    order = gateway.create_order(
        OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)
    )

    order_id, response = gateway.place_order(order)

    assert order_id == "12345"
    assert response == {"value": "12345"}


def test_create_order_generates_reserved_order_and_place_order_uses_same_id():
    gateway = FakeGateway(settings())
    client = FakeTradeClient()
    gateway._trade_client = client

    intent = OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)
    order = gateway.create_order(intent)
    gateway.place_order(order)

    assert order.order_id == "reserved-999"
    assert client.create_kwargs["account"] == "account-123"
    assert client.create_kwargs["contract"] is not None
    assert client.placed_order.order_id == "reserved-999"
    assert client.placed_order.action == "BUY"


def test_get_order_prefers_sdk_order_lookup():
    gateway = FakeGateway(settings())
    client = FakeTradeClient()
    gateway._trade_client = client

    order = gateway.get_order("reserved-999")

    assert order == {"id": "global-999", "order_id": "reserved-999"}
    assert client.get_order_kwargs == {"account": "account-123", "order_id": "reserved-999"}


def test_normalize_converts_nested_contract_object_to_plain_dict():
    normalized = normalize(FakeOrder())

    assert normalized["contract"] == {"symbol": "AAPL", "currency": "USD", "sec_type": "STK"}
    assert normalized["order_id"] == "broker-order-123"
    json.dumps(normalized)


def test_normalize_collection_converts_nested_contract_objects_for_orders_and_positions():
    orders = normalize_collection([FakeOrder()])
    positions = normalize_collection([FakePosition()])

    assert orders[0]["contract"] == {"symbol": "AAPL", "currency": "USD", "sec_type": "STK"}
    assert positions[0]["contract"] == {"symbol": "AAPL", "currency": "USD", "sec_type": "STK"}
    json.dumps(orders)
    json.dumps(positions)

from fastapi.testclient import TestClient

from tradehub.app import app, get_gateway, get_settings, get_store
from tradehub.config import Settings
from tradehub.tiger_gateway import normalize_collection

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
    """Mimics tigeropen Order.to_dict(), which embeds a raw Contract object under 'contract'."""

    def to_dict(self):
        return {"contract": FakeContract(), "order_id": "broker-order-123", "status": "SUBMITTED"}


class FakePosition:
    """Mimics tigeropen Position.to_dict(), which embeds a raw Contract object under 'contract'."""

    def to_dict(self):
        return {"contract": FakeContract(), "quantity": 10}


def settings():
    return Settings(TRADEHUB_API_TOKEN=STRONG_TOKEN)


def headers():
    return {"Authorization": f"Bearer {STRONG_TOKEN}"}


class UnconfiguredGateway:
    def is_configured(self):
        return False


class ConfiguredGateway:
    def __init__(self):
        self.positions_symbol = None
        self.orders_symbol = None
        self.orders_limit = None

    def is_configured(self):
        return True

    def get_assets(self):
        return {"cash": 1000}

    def get_positions(self, symbol=None):
        self.positions_symbol = symbol
        return [{"symbol": symbol or "AAPL", "quantity": 1}]

    def get_orders(self, symbol=None, limit=20):
        self.orders_symbol = symbol
        self.orders_limit = limit
        return [{"symbol": symbol or "AAPL", "limit": limit}]


class ExplodingGateway:
    def is_configured(self):
        return True

    def get_assets(self):
        raise RuntimeError(
            "broker failed for account sensitive-account with token "
            "test-token-with-enough-length and key sensitive-private-key"
        )


class FakeStore:
    def __init__(self):
        self.events = []

    def record_event(self, event_type, payload):
        self.events.append((event_type, payload))


def install_overrides(gateway, store=None, settings_override=settings):
    store = store or FakeStore()
    app.dependency_overrides[get_settings] = settings_override
    app.dependency_overrides[get_gateway] = lambda: gateway
    app.dependency_overrides[get_store] = lambda: store
    return store


def test_auth_accepts_valid_bearer_token():
    install_overrides(UnconfiguredGateway())
    try:
        response = TestClient(app).get("/health", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_auth_rejects_wrong_or_missing_bearer_token():
    install_overrides(UnconfiguredGateway())
    try:
        client = TestClient(app)
        wrong = client.get("/health", headers={"Authorization": "Bearer wrong-token"})
        missing = client.get("/health")
    finally:
        app.dependency_overrides.clear()

    assert wrong.status_code == 401
    assert missing.status_code == 401


def test_account_assets_handles_missing_tiger_credentials():
    install_overrides(UnconfiguredGateway())
    try:
        response = TestClient(app).get("/account/assets", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "tiger_configured": False,
        "assets": None,
        "warning": "Tiger credentials are not configured",
    }


def test_positions_are_read_only_and_can_filter_symbol():
    gateway = ConfiguredGateway()
    install_overrides(gateway)
    try:
        response = TestClient(app).get("/account/positions?symbol=aapl", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert gateway.positions_symbol == "aapl"
    assert response.json()["positions"] == [{"symbol": "aapl", "quantity": 1}]


def test_orders_with_contract_object_serialize_without_500():
    gateway = ConfiguredGateway()
    gateway.get_orders = lambda symbol=None, limit=20: normalize_collection([FakeOrder()])
    install_overrides(gateway)
    try:
        response = TestClient(app).get("/account/orders", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["orders"][0]["contract"] == {
        "symbol": "AAPL",
        "currency": "USD",
        "sec_type": "STK",
    }


def test_positions_with_contract_object_serialize_without_500():
    gateway = ConfiguredGateway()
    gateway.get_positions = lambda symbol=None: normalize_collection([FakePosition()])
    install_overrides(gateway)
    try:
        response = TestClient(app).get("/account/positions", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["positions"][0]["contract"] == {
        "symbol": "AAPL",
        "currency": "USD",
        "sec_type": "STK",
    }


def test_orders_limit_is_bounded():
    gateway = ConfiguredGateway()
    install_overrides(gateway)
    try:
        response = TestClient(app).get("/account/orders?limit=101", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_upstream_read_errors_are_sanitized():
    def sensitive_settings():
        return Settings(
            TRADEHUB_API_TOKEN=STRONG_TOKEN,
            TIGEROPEN_TIGER_ID="sensitive-tiger-id",
            TIGEROPEN_ACCOUNT="sensitive-account",
            TIGEROPEN_PRIVATE_KEY="sensitive-private-key",
        )

    store = install_overrides(ExplodingGateway(), settings_override=sensitive_settings)
    try:
        response = TestClient(app).get("/account/assets", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "upstream broker request failed"
    assert "sensitive-account" not in response.text
    assert "sensitive-private-key" not in response.text
    assert store.events[0][0] == "read_error"
    assert "sensitive-account" not in store.events[0][1]["reason"]
    assert "sensitive-private-key" not in store.events[0][1]["reason"]

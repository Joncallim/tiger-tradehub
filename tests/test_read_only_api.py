from fastapi.testclient import TestClient

from tradehub.app import app, get_gateway, get_settings, get_store
from tradehub.config import Settings


def settings():
    return Settings(TRADEHUB_API_TOKEN="test-token")


def headers():
    return {"Authorization": "Bearer test-token"}


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


class FakeStore:
    def record_event(self, event_type, payload):
        pass


def install_overrides(gateway):
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_gateway] = lambda: gateway
    app.dependency_overrides[get_store] = lambda: FakeStore()


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


def test_orders_limit_is_bounded():
    gateway = ConfiguredGateway()
    install_overrides(gateway)
    try:
        response = TestClient(app).get("/account/orders?limit=101", headers=headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422

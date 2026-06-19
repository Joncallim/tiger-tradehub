import sqlite3

from fastapi.testclient import TestClient

from tradehub.app import app, get_gateway, get_settings, get_store
from tradehub.audit import AuditStore
from tradehub.config import Settings

STRONG_TOKEN = "test-token-with-enough-length"


class FakeGateway:
    def __init__(self):
        self.placed = False
        self.fail_place = False
        self.fail_preview = False

    def is_configured(self):
        return True

    def preview_order(self, intent):
        if self.fail_preview:
            raise RuntimeError("preview failed for sensitive-account")
        return {"preview_symbol": intent.symbol}

    def place_order(self, intent):
        self.placed = True
        if self.fail_place:
            raise RuntimeError("temporary broker failure for sensitive-account")
        return "order-123", {"order_id": "order-123"}


def headers():
    return {"Authorization": f"Bearer {STRONG_TOKEN}"}


def install(settings, store, gateway):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_gateway] = lambda: gateway


def preview_payload():
    return {
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1,
        "order_type": "LIMIT",
        "limit_price": 150,
        "currency": "USD",
    }


def event_types(path):
    with sqlite3.connect(path) as db:
        return [
            row[0]
            for row in db.execute("SELECT event_type FROM audit_events ORDER BY id").fetchall()
        ]


def test_preview_and_dry_run_submit_create_and_finalize_confirmation(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(TRADEHUB_API_TOKEN=STRONG_TOKEN, TRADEHUB_DATABASE_PATH=db_path)
    store = AuditStore(db_path)
    gateway = FakeGateway()
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        preview = client.post("/orders/preview", json=preview_payload(), headers=headers())
        token = preview.json()["confirmation_token"]
        submit = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
        replay = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert preview.status_code == 200
    assert token
    assert submit.status_code == 200
    assert submit.json()["submitted"] is False
    assert gateway.placed is False
    assert replay.status_code == 422
    assert event_types(db_path) == ["preview_created", "dry_run_submit", "submit_block"]


def test_live_place_failure_releases_confirmation_for_retry(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
        TIGEROPEN_ACCOUNT="sensitive-account",
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    gateway.fail_place = True
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        preview = client.post("/orders/preview", json=preview_payload(), headers=headers())
        token = preview.json()["confirmation_token"]
        failed = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
        gateway.fail_place = False
        retried = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 502
    assert failed.json()["detail"]["message"] == "upstream broker request failed"
    assert "sensitive-account" not in failed.text
    assert retried.status_code == 200
    assert retried.json()["submitted"] is True
    assert event_types(db_path) == ["preview_created", "submit_error", "live_submit"]


def test_preview_failure_is_sanitized_and_does_not_create_token(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DATABASE_PATH=db_path,
        TIGEROPEN_ACCOUNT="sensitive-account",
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    gateway.fail_preview = True
    install(settings, store, gateway)

    try:
        response = TestClient(app).post(
            "/orders/preview", json=preview_payload(), headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "upstream broker request failed"
    assert "sensitive-account" not in response.text
    assert event_types(db_path) == ["preview_error"]


def test_submit_policy_revalidation_blocks_and_records_event(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings_state = {
        "value": Settings(
            TRADEHUB_API_TOKEN=STRONG_TOKEN,
            TRADEHUB_DATABASE_PATH=db_path,
            TRADEHUB_MAX_NOTIONAL_USD=1000,
        )
    }
    store = AuditStore(db_path)
    gateway = FakeGateway()
    app.dependency_overrides[get_settings] = lambda: settings_state["value"]
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_gateway] = lambda: gateway

    try:
        client = TestClient(app)
        preview = client.post("/orders/preview", json=preview_payload(), headers=headers())
        token = preview.json()["confirmation_token"]
        settings_state["value"] = Settings(
            TRADEHUB_API_TOKEN=STRONG_TOKEN,
            TRADEHUB_DATABASE_PATH=db_path,
            TRADEHUB_MAX_NOTIONAL_USD=100,
        )
        blocked = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert blocked.status_code == 422
    assert "notional" in blocked.json()["detail"]
    assert event_types(db_path) == ["preview_created", "submit_block"]

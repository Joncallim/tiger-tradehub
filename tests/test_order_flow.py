import sqlite3

from fastapi.testclient import TestClient

from tradehub.app import app, get_gateway, get_settings, get_store
from tradehub.audit import AuditStore
from tradehub.config import Settings

STRONG_TOKEN = "test-token-with-enough-length"


class FakeOrder:
    def __init__(self, order_id: str):
        self.order_id = order_id


class FakeGateway:
    def __init__(self):
        self.placed_orders = []
        self.created_orders = []
        self.fail_place = False
        self.fail_preview = False
        self.accept_and_fail = False
        self.fail_reconcile = False
        self.cancel_order_id = None
        self.broker_orders = {}

    def is_configured(self):
        return True

    def preview_order(self, intent):
        if self.fail_preview:
            raise RuntimeError("preview failed for sensitive-account")
        return {"preview_symbol": intent.symbol}

    def create_order(self, intent):
        order_id = f"reserved-{len(self.created_orders) + 1}"
        self.created_orders.append(order_id)
        return FakeOrder(order_id)

    def place_order(self, order):
        order_id = getattr(order, "order_id", None)
        self.placed_orders.append(order_id)
        if self.fail_place or self.accept_and_fail:
            if self.accept_and_fail and order_id is not None:
                self.broker_orders[str(order_id)] = {
                    "id": f"global-{order_id}",
                    "order_id": str(order_id),
                }
            raise RuntimeError("temporary broker failure for sensitive-account")
        if order_id is None:
            raise RuntimeError("missing order_id")
        order_response = {
            "id": f"global-{order_id}",
            "order_id": str(order_id),
        }
        self.broker_orders[str(order_id)] = order_response
        return f"global-{order_id}", order_response

    def get_order(self, order_id):
        if self.fail_reconcile:
            raise RuntimeError("broker reconciliation error for sensitive-account")
        return self.broker_orders.get(str(order_id))

    def assign_order_id(self, order, order_id):
        order.order_id = order_id
        return order

    def get_order_id(self, order):
        if isinstance(order, dict):
            return order.get("order_id") or order.get("id")
        return str(order.order_id)

    def cancel_order(self, order_id):
        self.cancel_order_id = order_id
        return {"cancelled": True}


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
    assert gateway.placed_orders == []
    assert replay.status_code == 422
    assert event_types(db_path) == ["preview_created", "dry_run_submit", "submit_block"]


def test_live_place_indeterminate_requires_reconcile_before_retry(tmp_path):
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
        replay = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 502
    assert failed.json()["detail"]["message"] == "upstream broker request failed"
    assert "sensitive-account" not in failed.text
    assert replay.status_code == 422
    assert "reconciled" in replay.json()["detail"] or "indeterminate" in replay.json()["detail"]
    assert event_types(db_path) == [
        "preview_created",
        "submit_indeterminate",
        "submit_block",
    ]


def test_reconcile_retry_allows_submit_with_same_reserved_number(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    gateway.fail_place = True
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]
        failed = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
        gateway.fail_place = False
        reconciled = client.post(
            "/orders/submit/reconcile", json={"confirmation_token": token}, headers=headers()
        )
        retried = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 502
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "retryable"
    assert retried.status_code == 200
    assert retried.json()["submitted"] is True
    assert len(gateway.placed_orders) == 2
    assert gateway.placed_orders[0] == gateway.placed_orders[1]
    assert event_types(db_path) == [
        "preview_created",
        "submit_indeterminate",
        "submit_reconcile_retryable",
        "live_submit",
    ]


def test_reconcile_with_existing_broker_order_prevents_duplicate_submit(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    gateway.accept_and_fail = True
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]
        failed = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
        reconciled = client.post(
            "/orders/submit/reconcile", json={"confirmation_token": token}, headers=headers()
        )
        duplicate_retry = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 502
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "resolved"
    assert duplicate_retry.status_code == 422
    assert gateway.placed_orders == ["reserved-1"]
    assert event_types(db_path) == [
        "preview_created",
        "submit_indeterminate",
        "submit_reconciled",
        "submit_block",
    ]


def test_reconcile_failure_stays_indeterminate(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
        TIGEROPEN_ACCOUNT="sensitive-account",
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    gateway.accept_and_fail = True
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]
        failed = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
        gateway.fail_reconcile = True
        reconciled = client.post(
            "/orders/submit/reconcile", json={"confirmation_token": token}, headers=headers()
        )
        replay = client.post(
            "/orders/submit", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert failed.status_code == 502
    assert reconciled.status_code == 502
    assert replay.status_code == 422
    assert "indeterminate" in replay.json()["detail"]
    assert event_types(db_path) == [
        "preview_created",
        "submit_indeterminate",
        "reconcile_error",
        "submit_block",
    ]


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


def test_live_cancel_forwards_recorded_order_id_to_gateway_and_records_event(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        cancel = client.post(
            "/orders/cancel", json={"order_id": "44386595912828928"}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True
    assert gateway.cancel_order_id == "44386595912828928"
    assert event_types(db_path) == ["cancel"]

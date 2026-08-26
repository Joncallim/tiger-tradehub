import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient

from tradehub.app import app, get_gateway, get_settings, get_store
from tradehub.audit import STALE_CLAIM_SECONDS, AuditStore, utc_now
from tradehub.config import Settings
from tradehub.models import OrderIntent

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
        self.preview_result = None
        self.accept_and_fail = False
        self.fail_reconcile = False
        self.get_order_results = None
        self.get_orders_responses = None
        self.cancel_order_id = None
        self.place_order_blocked = threading.Event()
        self.place_order_release = threading.Event()
        self.broker_orders = {}
        self.get_orders_calls = 0

    def is_configured(self):
        return True

    def preview_order(self, intent):
        if self.fail_preview:
            raise RuntimeError("preview failed for sensitive-account")
        if self.preview_result is not None:
            return self.preview_result
        return {
            "init_margin_before": 0,
            "init_margin": 0,
            "maint_margin_before": 0,
            "maint_margin": 0,
            "margin_currency": "USD",
            "equity_with_loan_before": 100000,
            "equity_with_loan": 100000,
            "min_commission": 0,
            "max_commission": 0,
            "commission_currency": "USD",
        }

    def create_order(self, intent):
        order_id = f"reserved-{len(self.created_orders) + 1}"
        self.created_orders.append(order_id)
        return FakeOrder(order_id)

    def place_order(self, order):
        order_id = getattr(order, "order_id", None)
        self.placed_orders.append(order_id)
        self.place_order_blocked.set()
        if not self.place_order_release.is_set():
            self.place_order_release.wait(2)
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
        if isinstance(self.get_order_results, list) and self.get_order_results:
            return self.get_order_results.pop(0)
        if self.fail_reconcile:
            raise RuntimeError("broker reconciliation error for sensitive-account")
        return self.broker_orders.get(str(order_id))

    def get_orders(self, symbol=None, limit=20):
        self.get_orders_calls += 1
        if isinstance(self.get_orders_responses, list):
            return self.get_orders_responses
        return []

    def assign_order_id(self, order, order_id):
        order.order_id = order_id
        return order

    def get_order_id(self, order):
        if isinstance(order, dict):
            return order.get("order_id") or order.get("id")
        return str(order.order_id)

    def get_global_order_id(self, order):
        value = order.get("id") if isinstance(order, dict) else None
        return str(value) if value is not None else None

    def cancel_order(self, order_id):
        self.cancel_order_id = order_id
        return {"cancelled": True}


def headers():
    return {"Authorization": f"Bearer {STRONG_TOKEN}"}


def install(settings, store, gateway, preserve_preview=False):
    if not preserve_preview:
        settings.preview_api_token = settings.api_token
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


def test_rejected_or_unknown_preview_mints_no_confirmation(tmp_path):
    for broker_preview in ({"warning_text": "order rejected"}, {"unexpected": True}):
        db_path = tmp_path / ("rejected.db" if "warning_text" in broker_preview else "unknown.db")
        settings = Settings(TRADEHUB_API_TOKEN=STRONG_TOKEN, TRADEHUB_DATABASE_PATH=db_path)
        store = AuditStore(db_path)
        gateway = FakeGateway()
        gateway.preview_result = broker_preview
        install(settings, store, gateway)
        try:
            response = TestClient(app).post(
                "/orders/preview", json=preview_payload(), headers=headers()
            )
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.json()["accepted"] is False
        assert response.json()["confirmation_token"] is None
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT COUNT(*) FROM confirmations").fetchone()[0] == 0


def test_preview_capability_cannot_submit(tmp_path):
    db_path = tmp_path / "capability.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_PREVIEW_API_TOKEN="preview-token-with-enough-length",
        TRADEHUB_DATABASE_PATH=db_path,
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    install(settings, store, gateway, preserve_preview=True)
    try:
        client = TestClient(app)
        preview = client.post(
            "/orders/preview",
            json=preview_payload(),
            headers={"Authorization": "Bearer preview-token-with-enough-length"},
        )
        submit = client.post(
            "/orders/submit",
            json={"confirmation_token": "not-a-real-token-value"},
            headers={"Authorization": "Bearer preview-token-with-enough-length"},
        )
    finally:
        app.dependency_overrides.clear()
    assert preview.status_code == 200
    assert submit.status_code == 401


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


def test_reconcile_persists_global_id_and_keeps_reserved_id_for_lookup(tmp_path):
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
        client.post("/orders/submit", json={"confirmation_token": token}, headers=headers())
        gateway.broker_orders["reserved-1"] = {
            "id": "global-123",
            "order_id": "reserved-456",
        }
        reconciled = client.post(
            "/orders/submit/reconcile",
            json={"confirmation_token": token},
            headers=headers(),
        )
        cancelled = client.post(
            "/orders/cancel", json={"order_id": "global-123"}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    with sqlite3.connect(db_path) as db:
        identifiers = db.execute(
            "SELECT order_id, reserved_order_id FROM confirmations WHERE token = ?", (token,)
        ).fetchone()
    assert reconciled.status_code == 200
    assert reconciled.json()["order_id"] == "global-123"
    assert identifiers == ("global-123", "reserved-1")
    assert cancelled.status_code == 200
    assert gateway.cancel_order_id == "global-123"


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


def test_missing_reserved_id_reports_manual_resolution_and_admin_resolves(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    install(settings, store, gateway)
    token, _ = store.create_confirmation(OrderIntent(**preview_payload()), None, ttl_seconds=300)
    *_, submit_lease = store.claim_confirmation(token)
    store.mark_submission_indeterminate(token, submit_lease)

    try:
        client = TestClient(app)
        reconciled = client.post(
            "/orders/submit/reconcile",
            json={"confirmation_token": token},
            headers=headers(),
        )
        resolved = client.post(
            "/orders/submit/resolve",
            json={
                "confirmation_token": token,
                "resolver": "on-call@example",
                "global_order_id": "global-verified",
            },
            headers=headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "manual_reconciliation_required"
    assert store.get_submission_state(token) == "SUBMITTED"
    assert resolved.status_code == 200
    assert resolved.json() == {
        "status": "submitted",
        "submitted": True,
        "order_id": "global-verified",
    }
    assert "manual_reconciliation_required" in event_types(db_path)
    assert "submit_manual_resolution" in event_types(db_path)


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
            TRADEHUB_PREVIEW_API_TOKEN=STRONG_TOKEN,
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


def test_submit_crash_point_2_reconcile_recovers_submit_before_finalize(tmp_path):
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
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]
        *_, submit_lease = store.claim_confirmation(token)
        gateway.placed_orders = ["reserved-1"]
        store.record_reserved_order_id(token, "reserved-1", submit_lease)
        store.mark_submission_in_progress(token, "reserved-1", submit_lease)
        gateway.broker_orders = {
            "reserved-1": {"id": "global-reserved-1", "order_id": "reserved-1"}
        }
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
                ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
            )
        reconciled = client.post(
            "/orders/submit/reconcile", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "resolved"
    assert reconciled.json()["order_id"] == "global-reserved-1"


def test_reconcile_retry_uses_bounded_retry_lookup(tmp_path):
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
        client.post("/orders/submit", json={"confirmation_token": token}, headers=headers())
        gateway.get_order_results = [
            None,
            {"id": "global-reserved-1", "order_id": "reserved-1"},
        ]
        reconciled = client.post(
            "/orders/submit/reconcile", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "resolved"
    assert reconciled.json()["order_id"] == "global-reserved-1"


def test_stolen_submitting_negative_reconcile_does_not_become_retryable(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )
    store = AuditStore(db_path)
    gateway = FakeGateway()
    install(settings, store, gateway)
    token, _ = store.create_confirmation(OrderIntent(**preview_payload()), None, ttl_seconds=300)
    *_, submit_lease = store.claim_confirmation(token)
    store.record_reserved_order_id(token, "reserved-1", submit_lease)
    store.mark_submission_in_progress(token, "reserved-1", submit_lease)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )

    try:
        reconciled = TestClient(app).post(
            "/orders/submit/reconcile",
            json={"confirmation_token": token},
            headers=headers(),
        )
    finally:
        app.dependency_overrides.clear()

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "indeterminate"
    assert store.get_submission_state(token) == "INDETERMINATE"


def test_transitive_reconcile_steal_keeps_live_submit_non_retryable(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )

    class TransitiveStealGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.first_lookup_blocked = threading.Event()
            self.first_lookup_release = threading.Event()
            self.lookup_lock = threading.Lock()
            self.lookup_calls = 0

        def get_order(self, order_id):
            with self.lookup_lock:
                self.lookup_calls += 1
                call_number = self.lookup_calls
            if call_number == 1:
                self.first_lookup_blocked.set()
                self.first_lookup_release.wait(2)
            return None

    store = AuditStore(db_path)
    gateway = TransitiveStealGateway()
    install(settings, store, gateway)

    def expire_claim():
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
                ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
            )

    try:
        client = TestClient(app)
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            submit_future = executor.submit(
                client.post,
                "/orders/submit",
                json={"confirmation_token": token},
                headers=headers(),
            )
            assert gateway.place_order_blocked.wait(2)
            expire_claim()

            reconcile_b_future = executor.submit(
                client.post,
                "/orders/submit/reconcile",
                json={"confirmation_token": token},
                headers=headers(),
            )
            assert gateway.first_lookup_blocked.wait(2)
            expire_claim()

            reconcile_c = client.post(
                "/orders/submit/reconcile",
                json={"confirmation_token": token},
                headers=headers(),
            )
            refused_submit = client.post(
                "/orders/submit", json={"confirmation_token": token}, headers=headers()
            )
            resolved = client.post(
                "/orders/submit/resolve",
                json={
                    "confirmation_token": token,
                    "resolver": "operator@example",
                    "global_order_id": "global-resolved",
                },
                headers=headers(),
            )

            gateway.first_lookup_release.set()
            gateway.place_order_release.set()
            reconcile_b = reconcile_b_future.result(timeout=10)
            submit_a = submit_future.result(timeout=10)
    finally:
        gateway.first_lookup_release.set()
        gateway.place_order_release.set()
        app.dependency_overrides.clear()

    assert reconcile_c.status_code == 200
    assert reconcile_c.json()["status"] == "indeterminate"
    assert refused_submit.status_code == 422
    assert "indeterminate" in refused_submit.json()["detail"]
    assert resolved.status_code == 200
    assert resolved.json() == {
        "status": "submitted",
        "submitted": True,
        "order_id": "global-resolved",
    }
    assert reconcile_b.status_code == 409
    assert submit_a.status_code == 409
    assert gateway.placed_orders == ["reserved-1"]
    assert store.get_submission_state(token) == "SUBMITTED"


def test_reconcile_fallback_scan_is_used_before_retryable(tmp_path):
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
        client.post("/orders/submit", json={"confirmation_token": token}, headers=headers())
        gateway.get_order_results = [None, None]
        reconciled = client.post(
            "/orders/submit/reconcile", json={"confirmation_token": token}, headers=headers()
        )
    finally:
        app.dependency_overrides.clear()

    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "retryable"
    assert gateway.get_orders_calls == 1


def test_concurrent_submit_is_rejected_while_first_submit_is_in_flight(tmp_path):
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
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]

        def submit():
            return client.post(
                "/orders/submit", json={"confirmation_token": token}, headers=headers()
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(submit)
            gateway.place_order_blocked.wait(2)
            second = executor.submit(submit)
            second_response = second.result(timeout=1)
            gateway.place_order_release.set()
            first_response = first.result(timeout=10)
    finally:
        app.dependency_overrides.clear()

    assert first_response.status_code == 200
    assert second_response.status_code == 422
    assert "indeterminate" in second_response.json()["detail"]


def test_concurrent_reconcile_calls_do_not_double_finalize(tmp_path):
    db_path = tmp_path / "tradehub.db"
    settings = Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_DRY_RUN=False,
        TRADEHUB_DATABASE_PATH=db_path,
    )

    class BlockingGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.call_started = threading.Event()
            self.call_release = threading.Event()

        def get_order(self, order_id):
            self.call_started.set()
            if not self.call_release.wait(2):
                raise RuntimeError("timeout")
            return super().get_order(order_id)

    gateway = BlockingGateway()
    gateway.accept_and_fail = True
    store = AuditStore(db_path)
    install(settings, store, gateway)

    try:
        client = TestClient(app)
        token = client.post("/orders/preview", json=preview_payload(), headers=headers()).json()[
            "confirmation_token"
        ]
        client.post("/orders/submit", json={"confirmation_token": token}, headers=headers())

        def reconcile():
            return client.post(
                "/orders/submit/reconcile",
                json={"confirmation_token": token},
                headers=headers(),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(reconcile)
            gateway.call_started.wait(2)
            second = executor.submit(reconcile)
            gateway.call_release.set()
            first_response = first.result(timeout=10)
            second_response = second.result(timeout=10)
    finally:
        app.dependency_overrides.clear()

    assert first_response.status_code == 200
    assert second_response.status_code in (409, 422)
    assert second_response.status_code != 500
    assert first_response.json()["status"] == "resolved"
    assert second_response.json()["detail"]
    assert store.get_submission_state(token) == "SUBMITTED"

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradehub.audit import AuditStore
from tradehub.models import OrderIntent


def intent():
    return OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)


def test_create_confirmation_records_preview_and_claim_returns_intent(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")

    token, expires_at = store.create_confirmation(intent(), {"preview": "ok"}, ttl_seconds=300)
    claimed_intent, preview = store.claim_confirmation(token)

    assert token
    assert expires_at
    assert claimed_intent.symbol == "AAPL"
    assert preview == {"preview": "ok"}

    with sqlite3.connect(tmp_path / "tradehub.db") as db:
        event_type = db.execute("SELECT event_type FROM audit_events").fetchone()[0]
    assert event_type == "preview_created"


def test_claim_is_single_use_until_released_or_finalized(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)

    store.claim_confirmation(token)
    with pytest.raises(ValueError, match="already being submitted"):
        store.claim_confirmation(token)

    store.release_confirmation(token)
    store.claim_confirmation(token)
    store.finalize_confirmation(token, order_id="123")

    with pytest.raises(ValueError, match="already been submitted"):
        store.claim_confirmation(token)


def test_concurrent_claim_allows_only_one_submitter(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)

    def claim():
        try:
            store.claim_confirmation(token)
            return "ok"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert results.count("ok") == 1
    assert any("already being submitted" in result for result in results)


def test_claim_rejects_unknown_and_expired_tokens(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=-1)

    with pytest.raises(KeyError, match="unknown"):
        store.claim_confirmation("missing-token")
    with pytest.raises(ValueError, match="expired"):
        store.claim_confirmation(token)

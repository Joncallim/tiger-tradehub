import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from tradehub.audit import (
    CONFIRMATION_STATE_SUBMITTING,
    STALE_CLAIM_SECONDS,
    AuditStore,
    utc_now,
)
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


def test_stale_claim_can_be_reclaimed(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    store.claim_confirmation(token)
    stale_claimed_at = utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)

    with sqlite3.connect(tmp_path / "tradehub.db") as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            (stale_claimed_at.isoformat(), token),
        )

    reclaimed_intent, _ = store.claim_confirmation(token)

    assert reclaimed_intent.symbol == "AAPL"


def test_stale_indeterminate_confirmation_is_not_reclaimable(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    store.claim_confirmation(token)
    store.mark_submission_indeterminate(token, reserved_order_id="order-001")

    stale_claimed_at = utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)
    with sqlite3.connect(tmp_path / "tradehub.db") as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            (stale_claimed_at.isoformat(), token),
        )

    with pytest.raises(ValueError, match="indeterminate"):
        store.claim_confirmation(token)


def test_claim_rejects_unknown_and_expired_tokens(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=-1)

    with pytest.raises(KeyError, match="unknown"):
        store.claim_confirmation("missing-token")
    with pytest.raises(ValueError, match="expired"):
        store.claim_confirmation(token)


def test_crash_restart_preserves_indeterminate_state(tmp_path):
    db_path = tmp_path / "tradehub.db"
    token, _ = AuditStore(db_path).create_confirmation(intent(), None, ttl_seconds=300)
    first_store = AuditStore(db_path)
    first_store.claim_confirmation(token)
    first_store.mark_submission_indeterminate(token, reserved_order_id="order-002")

    fresh_store = AuditStore(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )

    with pytest.raises(ValueError, match="indeterminate"):
        fresh_store.claim_confirmation(token)


def test_recorded_reserved_order_id_is_retrieved(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    store.record_reserved_order_id(token, "reserved-42")

    _, _, reserved_order_id = store.get_confirmation(token)

    assert reserved_order_id == "reserved-42"


def test_mark_submission_in_progress_prevents_reclaim_before_finalize(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)

    store.claim_confirmation(token)
    store.record_reserved_order_id(token, "reserved-42")
    store.mark_submission_in_progress(token, "reserved-42")

    with sqlite3.connect(tmp_path / "tradehub.db") as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )

    with pytest.raises(ValueError, match="indeterminate"):
        store.claim_confirmation(token)

    assert store.get_submission_state(token) == CONFIRMATION_STATE_SUBMITTING

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from tradehub.audit import (
    CONFIRMATION_STATE_INDETERMINATE,
    CONFIRMATION_STATE_RECONCILING,
    CONFIRMATION_STATE_SUBMITTING,
    STALE_CLAIM_SECONDS,
    AuditStore,
    utc_now,
)
from tradehub.models import OrderIntent


def intent():
    return OrderIntent(symbol="AAPL", side="BUY", quantity=1, limit_price=150)


def intent_with_client_request_id(client_request_id: str):
    return OrderIntent(
        symbol="AAPL",
        side="BUY",
        quantity=1,
        limit_price=150,
        client_request_id=client_request_id,
    )


def test_find_active_confirmation_by_client_request_id_recovers_after_restart(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(
        intent_with_client_request_id("proposal-1"), None, ttl_seconds=300
    )

    recovered = store.find_active_confirmation_by_client_request_id("proposal-1")

    assert recovered is not None
    recovered_token, recovered_intent, submission_state = recovered
    assert recovered_token == token
    assert recovered_intent.client_request_id == "proposal-1"


def test_find_active_confirmation_returns_none_when_absent(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    assert store.find_active_confirmation_by_client_request_id("missing") is None


def test_find_active_confirmation_ignores_submitted_confirmations(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(
        intent_with_client_request_id("proposal-2"), None, ttl_seconds=300
    )
    *_, lease_id = store.claim_confirmation(token)
    store.finalize_confirmation(token, order_id="broker-1", submit_lease_id=lease_id)

    assert store.find_active_confirmation_by_client_request_id("proposal-2") is None


def test_find_active_confirmation_fails_closed_on_ambiguity(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    store.create_confirmation(intent_with_client_request_id("dup"), None, ttl_seconds=300)
    store.create_confirmation(intent_with_client_request_id("dup"), None, ttl_seconds=300)

    with pytest.raises(ValueError, match="multiple active confirmations"):
        store.find_active_confirmation_by_client_request_id("dup")


def test_create_confirmation_records_preview_and_claim_returns_intent(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")

    token, expires_at = store.create_confirmation(intent(), {"preview": "ok"}, ttl_seconds=300)
    claimed_intent, preview, lease_id = store.claim_confirmation(token)

    assert token
    assert expires_at
    assert claimed_intent.symbol == "AAPL"
    assert preview == {"preview": "ok"}
    assert lease_id

    with sqlite3.connect(tmp_path / "tradehub.db") as db:
        event_type = db.execute("SELECT event_type FROM audit_events").fetchone()[0]
    assert event_type == "preview_created"


def test_claim_is_single_use_until_released_or_finalized(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)

    *_, first_lease = store.claim_confirmation(token)
    with pytest.raises(ValueError, match="already being submitted"):
        store.claim_confirmation(token)

    store.release_confirmation(token, first_lease)
    *_, second_lease = store.claim_confirmation(token)
    store.finalize_confirmation(token, order_id="123", submit_lease_id=second_lease)

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

    reclaimed_intent, _, _ = store.claim_confirmation(token)

    assert reclaimed_intent.symbol == "AAPL"


def test_stale_indeterminate_confirmation_is_not_reclaimable(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, lease_id = store.claim_confirmation(token)
    store.mark_submission_indeterminate(token, lease_id, reserved_order_id="order-001")

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
    *_, lease_id = first_store.claim_confirmation(token)
    first_store.mark_submission_indeterminate(token, lease_id, reserved_order_id="order-002")

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
    *_, lease_id = store.claim_confirmation(token)
    store.record_reserved_order_id(token, "reserved-42", lease_id)

    _, _, reserved_order_id = store.get_confirmation(token)

    assert reserved_order_id == "reserved-42"


def test_mark_submission_in_progress_prevents_reclaim_before_finalize(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)

    *_, lease_id = store.claim_confirmation(token)
    store.record_reserved_order_id(token, "reserved-42", lease_id)
    store.mark_submission_in_progress(token, "reserved-42", lease_id)

    with sqlite3.connect(tmp_path / "tradehub.db") as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )

    with pytest.raises(ValueError, match="indeterminate"):
        store.claim_confirmation(token)

    assert store.get_submission_state(token) == CONFIRMATION_STATE_SUBMITTING


def test_stale_reconciliation_reclaim_fences_old_lease_transitions(tmp_path):
    db_path = tmp_path / "tradehub.db"
    store = AuditStore(db_path)
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, submit_lease = store.claim_confirmation(token)
    store.mark_submission_indeterminate(token, submit_lease, reserved_order_id="reserved-42")

    *_, first_lease, _ = store.claim_reconciliation_confirmation(token)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )
    *_, second_lease, _ = store.claim_reconciliation_confirmation(token)

    assert first_lease != second_lease
    with pytest.raises(ValueError, match="not claimed"):
        store.finalize_confirmation(token, "global-42", reconcile_lease_id=first_lease)
    with pytest.raises(ValueError, match="reconciliation-required"):
        store.mark_submission_ready_for_retry(token, first_lease)
    with pytest.raises(ValueError, match="no longer current"):
        store.abandon_reconciliation(token, first_lease)
    with pytest.raises(ValueError, match="not claimed"):
        store.mark_submission_indeterminate(token, submit_lease, "reserved-42")

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT submission_state, reconcile_lease_id FROM confirmations WHERE token = ?",
            (token,),
        ).fetchone()
    assert row == (CONFIRMATION_STATE_RECONCILING, second_lease)
    store.mark_submission_ready_for_retry(token, second_lease)


def test_legacy_claimed_row_migrates_to_indeterminate_and_only_reconciles(tmp_path):
    db_path = tmp_path / "tradehub.db"
    now = utc_now()
    stale_claimed_at = now - timedelta(seconds=STALE_CLAIM_SECONDS + 1)
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE confirmations (
                token TEXT PRIMARY KEY,
                intent_json TEXT NOT NULL,
                tiger_preview_json TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                claimed_at TEXT,
                submitted_at TEXT,
                order_id TEXT
            )
            """
        )
        db.execute(
            """
            INSERT INTO confirmations(
                token, intent_json, created_at, expires_at, claimed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy-token",
                intent().model_dump_json(),
                now.isoformat(),
                (now + timedelta(seconds=300)).isoformat(),
                stale_claimed_at.isoformat(),
            ),
        )

    store = AuditStore(db_path)

    assert store.get_submission_state("legacy-token") == CONFIRMATION_STATE_INDETERMINATE
    with pytest.raises(ValueError, match="indeterminate"):
        store.claim_confirmation("legacy-token")
    claimed_intent, _, reserved_order_id, lease_id, prior_state = (
        store.claim_reconciliation_confirmation("legacy-token")
    )
    assert claimed_intent.symbol == "AAPL"
    assert reserved_order_id is None
    assert lease_id
    assert prior_state == CONFIRMATION_STATE_INDETERMINATE


def test_stolen_submit_lease_fences_old_worker_and_only_new_worker_finalizes(tmp_path):
    db_path = tmp_path / "tradehub.db"
    store = AuditStore(db_path)
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, worker_a_lease = store.claim_confirmation(token)
    store.record_reserved_order_id(token, "reserved-42", worker_a_lease)
    store.mark_submission_in_progress(token, "reserved-42", worker_a_lease)

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )
    *_, reconcile_lease, source_state = store.claim_reconciliation_confirmation(token)
    assert source_state == CONFIRMATION_STATE_SUBMITTING
    # This simulates an authoritative-negative reconciliation permitting retry.
    store.mark_submission_ready_for_retry(token, reconcile_lease)

    *_, worker_c_lease = store.claim_confirmation(token)
    store.mark_submission_in_progress(token, "reserved-42", worker_c_lease)
    with pytest.raises(ValueError, match="not claimed"):
        store.finalize_confirmation(token, "global-a", submit_lease_id=worker_a_lease)
    store.finalize_confirmation(token, "global-c", submit_lease_id=worker_c_lease)

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT submission_state, order_id, submitted_at FROM confirmations WHERE token = ?",
            (token,),
        ).fetchone()
    assert row[0:2] == ("SUBMITTED", "global-c")
    assert row[2] is not None


def test_stolen_submitting_without_order_stays_indeterminate_for_resolution(tmp_path):
    db_path = tmp_path / "tradehub.db"
    store = AuditStore(db_path)
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, submit_lease = store.claim_confirmation(token)
    store.mark_submission_in_progress(token, "reserved-42", submit_lease)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
            ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
        )

    *_, reconcile_lease, source_state = store.claim_reconciliation_confirmation(token)
    assert source_state == CONFIRMATION_STATE_SUBMITTING
    store.preserve_stolen_submission(token, reconcile_lease)

    assert store.get_submission_state(token) == CONFIRMATION_STATE_INDETERMINATE
    with pytest.raises(ValueError, match="indeterminate"):
        store.claim_confirmation(token)


def test_reconciliation_reclaims_preserve_original_submitting_lineage(tmp_path):
    db_path = tmp_path / "tradehub.db"
    store = AuditStore(db_path)
    token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, submit_lease = store.claim_confirmation(token)
    store.mark_submission_in_progress(token, "reserved-42", submit_lease)

    def expire_claim():
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE confirmations SET claimed_at = ? WHERE token = ?",
                ((utc_now() - timedelta(seconds=STALE_CLAIM_SECONDS + 1)).isoformat(), token),
            )

    expire_claim()
    *_, first_reconcile_lease, first_source_state = store.claim_reconciliation_confirmation(token)
    expire_claim()
    *_, second_reconcile_lease, second_source_state = store.claim_reconciliation_confirmation(token)

    assert first_reconcile_lease != second_reconcile_lease
    assert first_source_state == CONFIRMATION_STATE_SUBMITTING
    assert second_source_state == CONFIRMATION_STATE_SUBMITTING


def test_manual_resolution_supports_verified_submission_and_no_submission(tmp_path):
    store = AuditStore(tmp_path / "tradehub.db")
    submitted_token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, submitted_lease = store.claim_confirmation(submitted_token)
    store.mark_submission_indeterminate(submitted_token, submitted_lease)
    retry_token, _ = store.create_confirmation(intent(), None, ttl_seconds=300)
    *_, retry_lease = store.claim_confirmation(retry_token)
    store.mark_submission_indeterminate(retry_token, retry_lease)

    assert (
        store.resolve_indeterminate_confirmation(
            submitted_token, "operator@example", global_order_id="global-verified"
        )
        == "SUBMITTED"
    )
    assert (
        store.resolve_indeterminate_confirmation(
            retry_token, "operator@example", no_submission_occurred=True
        )
        == "READY"
    )
    assert store.get_submission_state(submitted_token) == "SUBMITTED"
    assert store.get_submission_state(retry_token) == "READY"

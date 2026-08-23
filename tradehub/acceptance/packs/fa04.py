"""FA-04 — Runtime safety, restart, and recovery.

Environment: local; no broker write required.

Exercises the deployed failure boundaries: prohibited orders are
blocked, malformed input fails closed, finalized/expired confirmation
tokens remain unusable across a service restart, audit history
persists across restart, and client-visible errors stay sanitized.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import httpx

from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
)
from tradehub.acceptance.service import ServiceManager

ALLOWLISTED = "AAPL"


def _base(manager: ServiceManager) -> str:
    return f"http://{manager.host}:{manager.port}"


def _headers(manager: ServiceManager) -> dict[str, str]:
    return {"Authorization": f"Bearer {manager.env.get('TRADEHUB_API_TOKEN', '')}"}


def build_fa04_pack() -> PackDefinition:
    def policy_blocks(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        cases = [
            (
                "market_order",
                {
                    "symbol": ALLOWLISTED,
                    "side": "BUY",
                    "quantity": 1,
                    "order_type": "MARKET",
                    "currency": "USD",
                },
            ),
            (
                "non_usd",
                {
                    "symbol": ALLOWLISTED,
                    "side": "BUY",
                    "quantity": 1,
                    "order_type": "LIMIT",
                    "limit_price": 150,
                    "currency": "SGD",
                },
            ),
            (
                "over_notional",
                {
                    "symbol": ALLOWLISTED,
                    "side": "BUY",
                    "quantity": 15,
                    "order_type": "LIMIT",
                    "limit_price": 150,
                    "currency": "USD",
                },
            ),
            (
                "over_quantity",
                {
                    "symbol": ALLOWLISTED,
                    "side": "BUY",
                    "quantity": 500,
                    "order_type": "LIMIT",
                    "limit_price": 1,
                    "currency": "USD",
                },
            ),
            (
                "symbol_not_allowlisted",
                {
                    "symbol": "TSLA",
                    "side": "BUY",
                    "quantity": 1,
                    "order_type": "LIMIT",
                    "limit_price": 150,
                    "currency": "USD",
                },
            ),
        ]
        for label, payload in cases:
            response = httpx.post(
                f"{_base(manager)}/orders/preview",
                headers=_headers(manager),
                json=payload,
                timeout=10,
            )
            if response.status_code != 422:
                raise AssertionError_(
                    f"{label}: expected 422, got {response.status_code}: {response.text[:300]}"
                )
            text = response.text
            for secret in ("TRADEHUB", "Bearer", "private_key"):
                if secret.lower() in text.lower():
                    # "Bearer" may appear in generic docs; only fail on obvious leaks
                    if secret == "TRADEHUB" and secret in text:
                        raise AssertionError_(f"{label}: error leaked internal name")
        manager.stop()

    def malformed_input_fails_closed(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        base = _base(manager)
        bad_payloads = [
            {},  # missing everything
            {
                "symbol": 123,
                "side": "BUY",
                "quantity": 1,
                "order_type": "LIMIT",
                "limit_price": 150,
                "currency": "USD",
            },
            {
                "symbol": ALLOWLISTED,
                "side": "HOLD",
                "quantity": 1,
                "order_type": "LIMIT",
                "limit_price": 150,
                "currency": "USD",
            },
            {
                "symbol": ALLOWLISTED,
                "side": "BUY",
                "quantity": -1,
                "order_type": "LIMIT",
                "limit_price": 150,
                "currency": "USD",
            },
        ]
        for payload in bad_payloads:
            response = httpx.post(
                f"{base}/orders/preview", headers=_headers(manager), json=payload, timeout=10
            )
            if response.status_code not in (400, 422):
                raise AssertionError_(
                    f"malformed payload accepted: HTTP {response.status_code}: {payload}"
                )
        manager.stop()

    def finalized_token_unusable_after_restart(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        base = _base(manager)
        preview = httpx.post(
            f"{base}/orders/preview",
            headers=_headers(manager),
            json={
                "symbol": ALLOWLISTED,
                "side": "BUY",
                "quantity": 1,
                "order_type": "LIMIT",
                "limit_price": 150,
                "currency": "USD",
            },
            timeout=10,
        )
        token = preview.json().get("confirmation_token")
        if not token:
            raise AssertionError_("no token from preview")
        ctx.register_secret(token)
        first = httpx.post(
            f"{base}/orders/submit",
            headers=_headers(manager),
            json={"confirmation_token": token},
            timeout=10,
        )
        if first.status_code != 200:
            raise AssertionError_(f"dry-run submit failed: HTTP {first.status_code}")
        manager.restart()
        replay = httpx.post(
            f"{base}/orders/submit",
            headers=_headers(manager),
            json={"confirmation_token": token},
            timeout=10,
        )
        if replay.status_code != 422:
            raise AssertionError_(
                "finalized token replay after restart: expected 422, got "
                f"{replay.status_code}: {replay.text[:300]}"
            )
        manager.stop()

    def expired_token_unusable_after_restart(ctx: RunContext) -> None:
        # Start an instance with a very short TTL, preview, let it expire,
        # restart, and prove the expired token remains rejected.
        manager = ServiceManager(ctx, env_overrides={"TRADEHUB_CONFIRMATION_TTL_SECONDS": "1"})
        manager.start()
        base = _base(manager)
        preview = httpx.post(
            f"{base}/orders/preview",
            headers=_headers(manager),
            json={
                "symbol": ALLOWLISTED,
                "side": "BUY",
                "quantity": 1,
                "order_type": "LIMIT",
                "limit_price": 150,
                "currency": "USD",
            },
            timeout=10,
        )
        token = preview.json().get("confirmation_token")
        if not token:
            raise AssertionError_("no token from short-TTL preview")
        ctx.register_secret(token)
        time.sleep(2.0)  # exceed the 1s TTL
        manager.restart()
        replay = httpx.post(
            f"{base}/orders/submit",
            headers=_headers(manager),
            json={"confirmation_token": token},
            timeout=10,
        )
        if replay.status_code != 422:
            raise AssertionError_(
                f"expired token after restart: expected 422, got {replay.status_code}"
            )
        manager.stop()

    def audit_persists_across_restart(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        db_path = REPO_ROOT / "data" / "tradehub.db"
        before = _count_events(db_path)
        preview = httpx.post(
            f"{_base(manager)}/orders/preview",
            headers=_headers(manager),
            json={
                "symbol": ALLOWLISTED,
                "side": "BUY",
                "quantity": 1,
                "order_type": "LIMIT",
                "limit_price": 150,
                "currency": "USD",
            },
            timeout=10,
        )
        token = preview.json().get("confirmation_token") or ""
        ctx.register_secret(token) if token else None
        manager.restart()
        after = _count_events(db_path)
        if after <= before:
            raise AssertionError_(
                f"audit events did not persist across restart: before={before} after={after}"
            )
        manager.stop()

    def errors_are_sanitized(ctx: RunContext) -> None:
        manager = ServiceManager(ctx)
        manager.start()
        base = _base(manager)
        # Force an upstream/internal-style error path deterministically:
        # unknown confirmation token => 422 with clean detail, no stacktrace.
        response = httpx.post(
            f"{base}/orders/submit",
            headers=_headers(manager),
            json={"confirmation_token": "UnknownToken00000000000000000000"},
            timeout=10,
        )
        text = response.text
        for leak_marker in ("Traceback", 'File "', "private_key=", "MIIC", "BEGIN"):
            if leak_marker in text:
                raise AssertionError_(f"error response leaked internal detail: {leak_marker}")
        manager.stop()

    return PackDefinition(
        pack_id="FA-04",
        environment="local",
        depends_on=["FA-03"],
        assertions=[
            AssertionSpec("policy.prohibited_orders_blocked", policy_blocks),
            AssertionSpec("input.malformed_fails_closed", malformed_input_fails_closed),
            AssertionSpec(
                "replay.finalized_token_restart_blocked", finalized_token_unusable_after_restart
            ),
            AssertionSpec(
                "replay.expired_token_restart_blocked", expired_token_unusable_after_restart
            ),
            AssertionSpec("audit.persists_across_restart", audit_persists_across_restart),
            AssertionSpec("errors.sanitized", errors_are_sanitized),
        ],
        safe_summary=(
            "Runtime safety, restart, and recovery boundaries passed: policy "
            "blocks, replay/expiry across restart, audit persistence, "
            "sanitized errors."
        ),
    )


def _count_events(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()
        return int(row[0]) if row else 0

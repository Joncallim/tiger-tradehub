"""FA-05 — Tiger paper-account broker lifecycle.

Environment: paper (trusted host; ONLY core pack allowed to write).

Hard gates (each failure => BLOCKED, never a workaround):
- upstream packs FA-00..FA-04 passed on the same deployment lineage
  (checked against recorded run state in data/acceptance/);
- explicit acceptance paper-write flag is enabled locally and defaults
  false (`TRADEHUB_ACCEPTANCE_PAPER_WRITE=true`);
- broker-reported account profile says accountType=PAPER (queried from
  Tiger account information — NOT sandbox_debug, NOT account-number
  shape, NOT filenames or prose);
- acceptance-only stricter caps (quantity/notional) are respected;
- USD limit order on an allowlisted symbol only;
- the test limit is proven non-marketable at submission time from a
  current quote; if it cannot be proven, BLOCKED.

Lifecycle: read state -> preview -> submit (acceptance authority) ->
broker order id -> read back -> cancel -> read back -> reconcile audit
-> prove exactly one order created.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx

from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionBlocked,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
)
from tradehub.acceptance.service import ServiceManager, TigerAccountProof

ACCEPTANCE_WRITE_FLAG = "TRADEHUB_ACCEPTANCE_PAPER_WRITE"
ACCEPTANCE_SYMBOL = "AAPL"
ACCEPTANCE_MAX_QUANTITY = 1.0
ACCEPTANCE_MAX_NOTIONAL_USD = 100.0
STATE_FILE = REPO_ROOT / "data" / "acceptance" / "state.json"


def _flag_enabled() -> bool:
    return os.environ.get(ACCEPTANCE_WRITE_FLAG, "").strip().lower() == "true"


def _read_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def _upstream_packs_pass() -> list[str]:
    state = _read_state()
    missing: list[str] = []
    for pack_id in ("FA-00", "FA-01", "FA-02", "FA-03", "FA-04"):
        record = state.get(pack_id)
        if not record or record.get("status") != "PASS":
            missing.append(pack_id)
    return missing


def _quote_last_price(ctx: RunContext) -> float | None:
    """Fetch current market price via the Tiger quote API (read-only).

    Raises:
        AssertionBlocked if the broker denies market-data permission
        (deterministic prerequisite: US market data must be enabled for
        the OpenAPI account/device in the Developer Center).
        AssertionEscalate for anything unexpected.
    """
    from tigeropen.common.consts import Language
    from tigeropen.quote.quote_client import QuoteClient
    from tigeropen.tiger_open_config import TigerOpenClientConfig

    settings = ctx.settings
    config = TigerOpenClientConfig(sandbox_debug=settings.tiger_sandbox)
    config.tiger_id = settings.tiger_id or ""
    config.account = settings.tiger_account or ""
    if settings.tiger_license:
        config.license = settings.tiger_license
    config.language = Language.en_US
    if settings.tiger_private_key_path:
        from tigeropen.common.util.signature_utils import read_private_key

        config.private_key = read_private_key(str(settings.tiger_private_key_path))
    client = QuoteClient(config)
    try:
        client.grab_quote_permission()
        quotes = client.get_briefs([ACCEPTANCE_SYMBOL])
    except AssertionBlocked:
        raise
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "permission" in message.lower() and "4000" in message:
            raise AssertionBlocked(
                "Tiger OpenAPI US market-data permission is not enabled for this "
                "account/device (code 4000). Enable US stock L1 market data for "
                "the OpenAPI account in the Developer Center "
                "(developer.itigerup.com/profile) before FA-05 can prove a "
                "non-marketable limit."
            ) from exc
        from tradehub.acceptance.runner import AssertionEscalate

        raise AssertionEscalate(f"quote fetch failed unexpectedly: {message}") from exc
    if not quotes:
        return None
    quote = quotes[0]
    for attr in ("latest_price", "close", "last", "current"):
        value = getattr(quote, attr, None)
        if value not in (None, "-"):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def build_fa05_pack() -> PackDefinition:
    def gate_write_flag(ctx: RunContext) -> None:
        # Default is false; explicitly enabled for this run only.
        if not _flag_enabled():
            raise AssertionBlocked(
                f"{ACCEPTANCE_WRITE_FLAG} is not enabled; acceptance paper write refused"
            )

    def gate_upstream_lineage(ctx: RunContext) -> None:
        missing = _upstream_packs_pass()
        if missing:
            raise AssertionBlocked(f"upstream packs not PASS on this lineage: {', '.join(missing)}")

    def gate_paper_proof(ctx: RunContext) -> None:
        proof = TigerAccountProof(ctx)
        account = proof.prove_paper()
        ctx.register_secret(str(account))
        ctx.artifacts.append(f"paper_account_proven={account}")

    def gate_marketable_limit_proof(ctx: RunContext) -> None:
        last = _quote_last_price(ctx)
        if last is None:
            raise AssertionBlocked("cannot fetch current quote to prove non-marketable limit")
        # Non-marketable: a BUY limit strictly below the current market.
        limit = round(last * 0.5, 2)
        if limit <= 0:
            raise AssertionBlocked("derived limit price is not positive")
        if (
            ACCEPTANCE_MAX_NOTIONAL_USD
            and limit * ACCEPTANCE_MAX_QUANTITY > ACCEPTANCE_MAX_NOTIONAL_USD
        ):
            raise AssertionBlocked("test limit violates acceptance notional cap")
        ctx.artifacts.append(ctx.write_artifact("fa05-quote", {"last": last, "limit": limit}))

    def lifecycle(ctx: RunContext) -> None:
        # Re-verify safety gates immediately before any write authority:
        # broker-reported PAPER plus a deterministically non-marketable
        # limit. The write-capable service (dry_run=false) starts only
        # after both proofs succeed. No allowlist/notional/quantity policy
        # is loosened: AAPL must already be in the production allowlist and
        # acceptance caps are stricter.
        proof = TigerAccountProof(ctx)
        paper_account = proof.prove_paper()
        ctx.register_secret(str(paper_account))

        last = _quote_last_price(ctx)
        if last is None:
            raise AssertionBlocked(
                "cannot prove non-marketable limit at submission time (no quote)"
            )
        limit_price = round(last * 0.5, 2)
        if limit_price <= 0:
            raise AssertionBlocked("derived limit price is not positive")
        if (
            ACCEPTANCE_MAX_NOTIONAL_USD
            and limit_price * ACCEPTANCE_MAX_QUANTITY > ACCEPTANCE_MAX_NOTIONAL_USD
        ):
            raise AssertionBlocked("test limit violates acceptance notional cap")
        if ACCEPTANCE_SYMBOL not in ctx.settings.symbol_allowlist:
            raise AssertionBlocked(
                f"acceptance symbol {ACCEPTANCE_SYMBOL} not in production allowlist"
            )

        manager = ServiceManager(
            ctx,
            env_overrides={
                "TRADEHUB_DRY_RUN": "false",
            },
        )
        manager.start()
        base = f"http://{manager.host}:{manager.port}"
        headers = {"Authorization": f"Bearer {manager.env.get('TRADEHUB_API_TOKEN', '')}"}

        # 1. read current paper-account state
        before = httpx.get(f"{base}/account/orders", headers=headers, timeout=30)
        if before.status_code != 200:
            manager.stop()
            raise AssertionError_(f"pre-read account orders failed: HTTP {before.status_code}")
        before_ids = {o.get("id") for o in before.json().get("orders", [])}

        # 2. preview through the normal guarded path (non-marketable limit)
        preview_payload = {
            "symbol": ACCEPTANCE_SYMBOL,
            "side": "BUY",
            "quantity": ACCEPTANCE_MAX_QUANTITY,
            "order_type": "LIMIT",
            "limit_price": limit_price,
            "currency": "USD",
        }
        preview = httpx.post(
            f"{base}/orders/preview", headers=headers, json=preview_payload, timeout=30
        )
        if preview.status_code != 200:
            manager.stop()
            raise AssertionError_(
                f"preview failed: HTTP {preview.status_code}: {preview.text[:300]}"
            )
        token = preview.json().get("confirmation_token")
        if not token:
            manager.stop()
            raise AssertionError_("preview returned no confirmation token")
        ctx.register_secret(token)

        # 3. submit through acceptance authority (paper, dry_run=false)
        submit = httpx.post(
            f"{base}/orders/submit",
            headers=headers,
            json={"confirmation_token": token},
            timeout=60,
        )
        manager.stop()
        if submit.status_code != 200:
            raise AssertionError_(
                f"paper submit failed: HTTP {submit.status_code}: {submit.text[:500]}"
            )
        body = submit.json()
        order_id = body.get("order_id")
        if not order_id:
            raise AssertionError_(f"paper submit returned no broker order id: {body}")
        ctx.register_secret(str(order_id))

        # 4. read that order back
        manager.start()
        orders = httpx.get(f"{base}/account/orders", headers=headers, timeout=30)
        if orders.status_code != 200:
            manager.stop()
            raise AssertionError_(f"read-back failed: HTTP {orders.status_code}")
        found = [o for o in orders.json().get("orders", []) if str(o.get("id")) == str(order_id)]
        if not found:
            manager.stop()
            raise AssertionError_(f"broker order {order_id} not found in account orders")

        # 5. cancel it
        cancel = httpx.post(
            f"{base}/orders/cancel",
            headers=headers,
            json={"order_id": str(order_id)},
            timeout=30,
        )
        manager.stop()
        if cancel.status_code != 200:
            raise AssertionError_(
                "cancel failed: HTTP "
                f"{cancel.status_code}: {cancel.text[:500]} — paper order "
                f"{order_id} state must be checked"
            )
        if cancel.json().get("cancelled") is not True:
            raise AssertionError_(f"cancel not confirmed: {cancel.json()}")

        # 6. read back final state
        manager.start()
        final = httpx.get(f"{base}/account/orders", headers=headers, timeout=30)
        manager.stop()
        if final.status_code != 200:
            raise AssertionError_(f"final read-back failed: HTTP {final.status_code}")
        final_order = [
            o for o in final.json().get("orders", []) if str(o.get("id")) == str(order_id)
        ]
        if not final_order:
            raise AssertionError_(f"cancelled order {order_id} missing from read-back")

        # 7. reconcile audit events to the same broker order id
        db_path = REPO_ROOT / "data" / "tradehub.db"
        audit = _audit_events(db_path)
        live_subs = [e for e in audit if e["event_type"] == "live_submit"]
        cancels = [e for e in audit if e["event_type"] == "cancel"]
        if not any(str(e.get("payload", {}).get("order_id")) == str(order_id) for e in live_subs):
            raise AssertionError_(f"no live_submit audit event for order {order_id}")
        if not any(str(e.get("payload", {}).get("order_id")) == str(order_id) for e in cancels):
            raise AssertionError_(f"no cancel audit event for order {order_id}")

        # 8. exactly one intended broker order created by this run
        after_ids = {o.get("id") for o in final.json().get("orders", [])}
        new_ids = after_ids - before_ids
        if len(new_ids) != 1:
            raise AssertionError_(
                f"expected exactly one new order, found {len(new_ids)}: {new_ids}"
            )
        if order_id not in new_ids:
            raise AssertionError_("reconciled order id not among new orders")

        ctx.artifacts.append(ctx.write_artifact("fa05-lifecycle", {"order_id": str(order_id)}))

    return PackDefinition(
        pack_id="FA-05",
        environment="paper",
        depends_on=["FA-00", "FA-01", "FA-02", "FA-03", "FA-04"],
        assertions=[
            AssertionSpec("gate.acceptance_write_flag", gate_write_flag),
            AssertionSpec("gate.upstream_lineage", gate_upstream_lineage),
            AssertionSpec("gate.broker_paper_proof", gate_paper_proof),
            AssertionSpec("gate.non_marketable_proof", gate_marketable_limit_proof),
            AssertionSpec("lifecycle.place_read_cancel_reconcile", lifecycle),
        ],
        safe_summary=(
            "Tiger paper-account broker lifecycle passed: one small "
            "non-marketable limit order placed, read back, cancelled, and "
            "reconciled in audit."
        ),
    )


def _audit_events(db_path: Path) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT event_type, payload_json FROM audit_events ORDER BY id"
        ).fetchall()
    return [{"event_type": r[0], "payload": json.loads(r[1]) if r[1] else {}} for r in rows]

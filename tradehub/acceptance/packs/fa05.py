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
- USD limit order on an allowlisted symbol only.

Quote semantics (per program decision 2026-08-23):
- REAL-TIME US L1 quotes are NOT required for functional acceptance.
- The runner uses Tiger's freely available DELAYED US quote
  (`get_stock_delay_briefs`) only to choose a deliberately conservative
  paper-test limit. The delayed quote is NOT treated as current
  executable market data; results label it explicitly as delayed.
- Because PAPER is independently proven, an unexpected fill is not a
  real-money safety failure, but it IS an acceptance event that must be
  handled explicitly (recorded, reconciled, flattened if possible).

Acceptance limit rule (deterministic, runner-owned):
    acceptance_limit = delayed_price * 0.50 (rounded for the instrument)
The percentage may be adjusted only if the broker rejects an obviously
unreasonable limit due to price-band rules; the adjustment is documented,
the order stays intentionally far from the delayed reference, and the
runner never switches to MARKET nor increases quantity/notional to make
the test work.

Lifecycle: prove PAPER -> fetch delayed quote -> derive conservative
limit -> preview -> submit (acceptance authority) -> broker order id ->
read back -> cancel (or handle fill) -> read back -> reconcile audit ->
prove exactly one order created.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionBlocked,
    AssertionError_,
    AssertionEscalate,
    AssertionSpec,
    PackDefinition,
    RunContext,
)
from tradehub.acceptance.service import ServiceManager, TigerAccountProof

ACCEPTANCE_WRITE_FLAG = "TRADEHUB_ACCEPTANCE_PAPER_WRITE"
ACCEPTANCE_SYMBOL = "AAPL"
ACCEPTANCE_MAX_QUANTITY = 1.0
ACCEPTANCE_MAX_NOTIONAL_USD = 100.0
ACCEPTANCE_LIMIT_FRACTION = 0.50  # deterministic conservative limit rule
STATE_FILE = REPO_ROOT / "data" / "acceptance" / "state.json"

# Order statuses that indicate the paper order filled before cancellation.
FILLED_STATUSES = {"FILLED", "PARTIALLY_FILLED"}


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


def acceptance_limit_rule(
    delayed_price: float,
    quantity: float = ACCEPTANCE_MAX_QUANTITY,
    max_notional_usd: float = ACCEPTANCE_MAX_NOTIONAL_USD,
    fraction: float = ACCEPTANCE_LIMIT_FRACTION,
) -> tuple[float, float]:
    """Deterministic conservative limit from a delayed reference price.

    rule: limit = delayed_price * fraction, rounded to cents, strictly
    positive, and within the acceptance notional cap. When the base
    fraction would breach the notional cap (e.g. a high-priced symbol),
    the fraction is deterministically shrunk so the order still fits
    under the cap with margin while remaining deliberately far below the
    delayed reference. Returns (limit, fraction_used). Raises
    AssertionBlocked for unusable references.
    """
    if delayed_price is None or delayed_price <= 0:
        raise AssertionBlocked("delayed reference price is not positive")
    fraction_used = float(fraction)
    limit = round(delayed_price * fraction_used, 2)
    notional = limit * quantity
    if notional > max_notional_usd:
        # Runner-owned deterministic adjustment: shrink the fraction so the
        # derived order fits under the acceptance cap with margin. The
        # adjustment is documented in the artifact record; the runner never
        # raises the cap or switches to MARKET to make the test work.
        fraction_used = (max_notional_usd / (delayed_price * quantity)) * 0.8
        limit = round(delayed_price * fraction_used, 2)
        notional = limit * quantity
    if limit <= 0:
        raise AssertionBlocked("derived limit price is not positive")
    if notional > max_notional_usd:
        raise AssertionBlocked(
            f"derived notional {notional:.2f} exceeds acceptance cap {max_notional_usd:.2f}"
        )
    return limit, fraction_used


def derive_acceptance_limit(
    delayed_price: float,
    quantity: float = ACCEPTANCE_MAX_QUANTITY,
    max_notional_usd: float = ACCEPTANCE_MAX_NOTIONAL_USD,
    fraction: float = ACCEPTANCE_LIMIT_FRACTION,
) -> float:
    """Return just the limit (see acceptance_limit_rule)."""
    limit, _ = acceptance_limit_rule(
        delayed_price, quantity=quantity, max_notional_usd=max_notional_usd, fraction=fraction
    )
    return limit


def _delayed_quote_record(
    ctx: RunContext, delayed_price: float, quote_time_ms: int | None
) -> dict[str, object]:
    """Build the artifact record for the delayed quote reference.

    Labels the quote as DELAYED (never real-time), records retrieval
    timestamp and a staleness classification based on the quote's own
    timestamp when available.
    """
    retrieved_at = datetime.now(tz=timezone.utc)
    staleness = "unknown"
    if quote_time_ms:
        quote_time = datetime.fromtimestamp(quote_time_ms / 1000, tz=timezone.utc)
        age_seconds = max(0, int((retrieved_at - quote_time).total_seconds()))
        if age_seconds < 3600:
            staleness = f"delayed_under_1h({age_seconds}s)"
        elif age_seconds < 86400:
            staleness = f"delayed_1h_to_24h({age_seconds}s)"
        else:
            staleness = f"delayed_over_24h({age_seconds}s)"
    record: dict[str, object] = {
        "symbol": ACCEPTANCE_SYMBOL,
        "source": "tiger_delayed_quote",
        "classification": "DELAYED",
        "delayed_price": delayed_price,
        "quote_time_ms": quote_time_ms,
        "retrieved_at": retrieved_at.isoformat(),
        "staleness": staleness,
        "is_real_time": False,
    }
    return record


def _delayed_quote(ctx: RunContext) -> tuple[float, int | None]:
    """Fetch Tiger's freshest freely available DELAYED US quote.

    Uses get_stock_delay_briefs via a single reusable QuoteClient. A
    missing/denied delayed quote => BLOCKED (deterministic prerequisite);
    anything unexpected => ESCALATE. Never treats the result as real-time.
    """
    from tigeropen.common.consts import Language
    from tigeropen.common.util.signature_utils import read_private_key
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
        config.private_key = read_private_key(str(settings.tiger_private_key_path))

    # ONE reusable client per run: grab_quote_permission() transfers device
    # access and does NOT purchase permission — repeated grabs from fresh
    # clients cause device-access contention (per Tiger docs).
    client = getattr(ctx, "_quote_client", None)
    if client is None:
        client = QuoteClient(config)
        ctx._quote_client = client
        try:
            client.grab_quote_permission()
        except AssertionBlocked:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AssertionEscalate(f"grab_quote_permission failed: {exc}") from exc

    try:
        df = client.get_stock_delay_briefs([ACCEPTANCE_SYMBOL])
    except AssertionBlocked:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AssertionEscalate(f"delayed quote fetch failed: {exc}") from exc

    if df is None or df.empty:
        raise AssertionBlocked("delayed quote returned no rows")
    row = df.iloc[0]
    price = row.get("close", row.get("pre_close"))
    if price is None or str(price) in ("", "-") or float(price) <= 0:
        raise AssertionBlocked("delayed quote returned no usable price")
    quote_time = row.get("time")
    quote_time_ms = int(quote_time) if quote_time is not None else None
    return float(price), quote_time_ms


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

    def gate_delayed_reference(ctx: RunContext) -> None:
        price, quote_time_ms = _delayed_quote(ctx)
        limit, fraction_used = acceptance_limit_rule(price)
        record = _delayed_quote_record(ctx, price, quote_time_ms)
        record["acceptance_limit"] = limit
        record["limit_rule"] = (
            f"delayed_price * {fraction_used} "
            f"= {price} * {fraction_used} = {limit} "
            f"(base fraction {ACCEPTANCE_LIMIT_FRACTION}; deterministic "
            "adjustment applied if notional cap required it)"
        )
        ctx.artifacts.append(ctx.write_artifact("fa05-delayed-quote", record))

    def lifecycle(ctx: RunContext) -> None:
        # Re-verify safety gates immediately before any write authority:
        # broker-reported PAPER plus a fresh delayed reference. The
        # write-capable service (dry_run=false) starts only after both
        # succeed. No allowlist/notional/quantity policy is loosened.
        proof = TigerAccountProof(ctx)
        paper_account = proof.prove_paper()
        ctx.register_secret(str(paper_account))

        delayed_price, quote_time_ms = _delayed_quote(ctx)
        limit_price, fraction_used = acceptance_limit_rule(delayed_price)
        quote_record = _delayed_quote_record(ctx, delayed_price, quote_time_ms)
        quote_record["acceptance_limit"] = limit_price
        quote_record["limit_rule"] = (
            f"delayed_price * {fraction_used} "
            f"= {delayed_price} * {fraction_used} = {limit_price} "
            f"(base fraction {ACCEPTANCE_LIMIT_FRACTION}; deterministic "
            "adjustment applied if notional cap required it)"
        )
        ctx.register_secret(str(paper_account))

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
        before_ids = {str(o.get("id")) for o in before.json().get("orders", [])}

        # 2. preview through the normal guarded path (conservative limit)
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

        order_status = str(found[0].get("status", ""))
        filled_qty = float(found[0].get("filled") or 0)
        unexpected_fill = order_status in FILLED_STATUSES or (
            filled_qty > 0 and order_status not in ("CANCELLED", "EXPIRED", "REJECTED")
        )

        # 5a. cancel it (normal path) — if not unexpectedly filled
        if unexpected_fill:
            cancel_result: dict[str, object] | None = None
        else:
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
            cancel_result = {"cancelled": cancel.json().get("cancelled")}
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
        if not unexpected_fill and not any(
            str(e.get("payload", {}).get("order_id")) == str(order_id) for e in cancels
        ):
            raise AssertionError_(f"no cancel audit event for order {order_id}")

        # 8. exactly one intended broker order created by this run
        after_ids = {str(o.get("id")) for o in final.json().get("orders", [])}
        new_ids = after_ids - before_ids
        if len(new_ids) != 1:
            raise AssertionError_(
                f"expected exactly one new order, found {len(new_ids)}: {new_ids}"
            )
        if order_id not in new_ids:
            raise AssertionError_("reconciled order id not among new orders")
        if unexpected_fill:
            lifecycle_record: dict[str, object] = {
                "order_id": str(order_id),
                "unexpected_paper_fill": True,
                "order_status": order_status,
                "filled_quantity": filled_qty,
                "quote": quote_record,
                "note": (
                    "PAPER order filled before cancellation; cancellation path "
                    "was not exercised this run. Fill belongs to the intended "
                    "acceptance order and is fully reconciled; exactly one new "
                    "order was created. Because the account is broker-proven "
                    "PAPER, this is not a real-money safety event. Residual "
                    "paper position is surfaced here and NOT flattened "
                    "automatically: no market order and no live-account "
                    "interaction; a follow-up acceptance action (guarded paper "
                    "SELL) would be required to flatten."
                ),
            }
        else:
            lifecycle_record = {
                "order_id": str(order_id),
                "unexpected_paper_fill": False,
                "order_status": order_status,
                "cancel": cancel_result,
                "quote": quote_record,
                "note": "PAPER order placed and cancelled normally; no fill.",
            }
        ctx.artifacts.append(ctx.write_artifact("fa05-lifecycle", lifecycle_record))

    return PackDefinition(
        pack_id="FA-05",
        environment="paper",
        depends_on=["FA-00", "FA-01", "FA-02", "FA-03", "FA-04"],
        assertions=[
            AssertionSpec("gate.acceptance_write_flag", gate_write_flag),
            AssertionSpec("gate.upstream_lineage", gate_upstream_lineage),
            AssertionSpec("gate.broker_paper_proof", gate_paper_proof),
            AssertionSpec("gate.delayed_reference_limit", gate_delayed_reference),
            AssertionSpec("lifecycle.place_read_cancel_reconcile", lifecycle),
        ],
        safe_summary=(
            "Tiger paper-account broker lifecycle passed: broker-proven PAPER, "
            "delayed-quote conservative limit, one small limit order placed, "
            "read back, cancelled (or fill handled), and reconciled in audit."
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

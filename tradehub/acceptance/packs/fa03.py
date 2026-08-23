"""FA-03 — Guarded dry-run order lifecycle through MCP.

Environment: local; broker write prohibited.

Proves the full MCP -> REST -> policy -> confirmation -> audit path
while dry_run=true. The acceptance authority (the runner, not a relaxed
user-facing skill) permits the pack to call the MCP submit tool only
after proving dry_run=true and require_approval=true. Expected: submit
returns submitted=false/dry_run=true, replay is blocked, dry-run cancel
is recorded, and the audit sequence reconstructs the lifecycle.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tradehub.acceptance.mcp_client import call_tool
from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
)

PREVIEW_PAYLOAD = {
    "symbol": "AAPL",
    "side": "BUY",
    "quantity": 1,
    "order_type": "LIMIT",
    "limit_price": 150,
    "currency": "USD",
}


def _mcp_env(ctx: RunContext) -> dict[str, str]:
    from tradehub.acceptance.service import get_service

    manager = get_service(ctx)
    return {
        "TRADEHUB_BASE_URL": f"http://{manager.host}:{manager.port}",
        "TRADEHUB_API_TOKEN": manager.env.get("TRADEHUB_API_TOKEN", ""),
        "PATH": manager.env.get("PATH", ""),
    }


def build_fa03_pack() -> PackDefinition:
    def preflight_proves_safety(ctx: RunContext) -> None:
        from tradehub.acceptance.service import get_service

        manager = get_service(ctx)
        body = manager.health()
        if body.get("dry_run") is not True:
            raise AssertionError_(f"dry_run not true: {body}")
        if body.get("require_approval") is not True:
            raise AssertionError_(f"require_approval not true: {body}")

    def preview_through_mcp(ctx: RunContext) -> None:
        body = call_tool(ctx, "preview_order", PREVIEW_PAYLOAD, _mcp_env(ctx))
        if body.get("accepted") is not True:
            raise AssertionError_(f"preview not accepted: {body}")
        if not body.get("confirmation_token"):
            raise AssertionError_("preview returned no confirmation token")
        if not body.get("expires_at"):
            raise AssertionError_("preview returned no expiry")
        ctx.artifacts.append(ctx.write_artifact("fa03-preview", {"accepted": True}))
        # Token must stay internal to the runner, never in the report.
        ctx.register_secret(str(body["confirmation_token"]))

    def submit_through_mcp_dry_run(ctx: RunContext) -> None:
        # Re-preview to obtain the token internally within this assertion.
        body = call_tool(ctx, "preview_order", PREVIEW_PAYLOAD, _mcp_env(ctx))
        token = body.get("confirmation_token")
        if not token:
            raise AssertionError_("no confirmation token to submit")
        ctx.register_secret(str(token))
        result = call_tool(ctx, "submit_order", {"confirmation_token": token}, _mcp_env(ctx))
        if result.get("submitted") is not False:
            raise AssertionError_(f"dry-run submit reported submitted=true: {result}")
        if result.get("dry_run") is not True:
            raise AssertionError_(f"dry_run flag not true on submit: {result}")

    def replay_is_blocked(ctx: RunContext) -> None:
        body = call_tool(ctx, "preview_order", PREVIEW_PAYLOAD, _mcp_env(ctx))
        token = body.get("confirmation_token")
        if not token:
            raise AssertionError_("no token for replay test")
        ctx.register_secret(str(token))
        first = call_tool(ctx, "submit_order", {"confirmation_token": token}, _mcp_env(ctx))
        if first.get("submitted") is not False:
            raise AssertionError_("first submit unexpectedly submitted")
        second = call_tool(ctx, "submit_order", {"confirmation_token": token}, _mcp_env(ctx))
        if second.get("_isError") is not True:
            raise AssertionError_(f"replay was not blocked: {second}")

    def dry_run_cancel_records_event(ctx: RunContext) -> None:
        from tradehub.acceptance.service import get_service

        get_service(ctx)
        store_path = REPO_ROOT / "data" / "tradehub.db"
        before = _count_events(store_path, "dry_run_cancel")
        result = call_tool(ctx, "cancel_order", {"order_id": "test-order-nope"}, _mcp_env(ctx))
        after = _count_events(store_path, "dry_run_cancel")
        if after <= before:
            raise AssertionError_("dry_run_cancel event not recorded")
        if result.get("cancelled") is not False or result.get("dry_run") is not True:
            raise AssertionError_(f"dry-run cancel response unexpected: {result}")

    def audit_sequence_reconstructs(ctx: RunContext) -> None:
        from tradehub.acceptance.service import get_service

        get_service(ctx)
        events = _audit_events(REPO_ROOT / "data" / "tradehub.db")
        types = [e["event_type"] for e in events]
        for expected in ("preview_created", "dry_run_submit", "dry_run_cancel"):
            if expected not in types:
                raise AssertionError_(f"audit missing expected event {expected}: {types}")

    def report_has_no_token(ctx: RunContext) -> None:
        # The sanitizer must never let a 32-char token-shaped string into output.
        sample = {"confirmation_token": "AbCdEfGhIjKlMnOpQrStUvWxYz012345"}
        cleaned = ctx.sanitizer.sanitize_value(sample)
        if "AbCdEfGhIjKlMnOpQrStUvWxYz012345" in str(cleaned):
            raise AssertionError_("confirmation token leaked into report")

    return PackDefinition(
        pack_id="FA-03",
        environment="local",
        depends_on=["FA-02"],
        assertions=[
            AssertionSpec("preflight.dry_run_and_approval", preflight_proves_safety),
            AssertionSpec("preview.mcp_accepted", preview_through_mcp),
            AssertionSpec("submit.mcp_dry_run_refuses", submit_through_mcp_dry_run),
            AssertionSpec("replay.blocked", replay_is_blocked),
            AssertionSpec("cancel.dry_run_records_event", dry_run_cancel_records_event),
            AssertionSpec("audit.sequence_reconstructs", audit_sequence_reconstructs),
            AssertionSpec("report.no_confirmation_token", report_has_no_token),
        ],
        safe_summary=(
            "Guarded dry-run order lifecycle through MCP passed; no broker write occurred."
        ),
    )


def _count_events(db_path: Path, event_type: str) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = ?", (event_type,)
        ).fetchone()
        return int(row[0]) if row else 0


def _audit_events(db_path: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT event_type, payload_json FROM audit_events ORDER BY id"
        ).fetchall()
    return [{"event_type": r[0], "payload": json.loads(r[1]) if r[1] else {}} for r in rows]

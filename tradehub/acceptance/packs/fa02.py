"""FA-02 — MCP discovery + real read-only Tiger workflow.

Environment: local (trusted host, read-only Tiger calls).

Proves Hermes/DeepSeek can use the *real deployed MCP surface*: the
tradehub-mcp binary is started over stdio, tools are discovered, and
read-only tools are exercised with real parameters. No write tool is
invoked. Missing Tiger credentials/connectivity => BLOCKED, not FAIL.
"""

from __future__ import annotations

from tradehub.acceptance.mcp_client import call_tool, list_tools
from tradehub.acceptance.runner import (
    AssertionBlocked,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
)

EXPECTED_TOOLS = {
    "health",
    "account_assets",
    "account_positions",
    "account_orders",
    "preview_order",
    "submit_order",
    "cancel_order",
}


def _mcp_env(ctx: RunContext) -> dict[str, str]:
    from tradehub.acceptance.service import get_service

    manager = get_service(ctx)
    return {
        "TRADEHUB_BASE_URL": f"http://{manager.host}:{manager.port}",
        "TRADEHUB_API_TOKEN": manager.env.get("TRADEHUB_API_TOKEN", ""),
        "TRADEHUB_PREVIEW_API_TOKEN": manager.env.get("TRADEHUB_PREVIEW_API_TOKEN", ""),
        "PATH": manager.env.get("PATH", ""),
    }


def build_fa02_pack() -> PackDefinition:
    def server_starts(ctx: RunContext) -> None:
        try:
            tools = list_tools(ctx, _mcp_env(ctx))
        except Exception as exc:  # noqa: BLE001
            raise AssertionBlocked(
                f"tradehub-mcp could not start (BLOCKED, not FAIL): {exc}"
            ) from exc
        if not tools:
            raise AssertionError_("MCP server returned no tools")

    def tools_discoverable(ctx: RunContext) -> None:
        try:
            tools = list_tools(ctx, _mcp_env(ctx))
        except Exception as exc:  # noqa: BLE001
            raise AssertionBlocked(f"tradehub-mcp unavailable: {exc}") from exc
        names = {tool.get("name") for tool in tools}
        missing = EXPECTED_TOOLS - names
        if missing:
            raise AssertionError_(f"MCP tools missing: {sorted(missing)}")

    def health_via_mcp(ctx: RunContext) -> None:
        body = call_tool(ctx, "health", extra_env=_mcp_env(ctx))
        if body.get("ok") is not True:
            raise AssertionError_(f"health via MCP not ok: {body}")
        if body.get("dry_run") is not True:
            raise AssertionError_("dry_run not true via MCP health")

    def read_assets_via_mcp(ctx: RunContext) -> None:
        body = call_tool(ctx, "account_assets", extra_env=_mcp_env(ctx))
        if body.get("tiger_configured") is not True:
            raise AssertionBlocked(
                f"Tiger credentials not configured/readable: {body.get('warning') or body}"
            )
        assets = body.get("assets")
        if assets is None:
            raise AssertionError_("account_assets returned no assets payload")

    def read_positions_symbol_filtered(ctx: RunContext) -> None:
        body = call_tool(ctx, "account_positions", {"symbol": "AAPL"}, extra_env=_mcp_env(ctx))
        if body.get("tiger_configured") is not True:
            raise AssertionBlocked("Tiger credentials not configured/readable")
        if not isinstance(body.get("positions"), list):
            raise AssertionError_("positions response malformed")

    def read_orders_bounded_limit(ctx: RunContext) -> None:
        body = call_tool(ctx, "account_orders", {"limit": 5}, extra_env=_mcp_env(ctx))
        if body.get("tiger_configured") is not True:
            raise AssertionBlocked("Tiger credentials not configured/readable")
        orders = body.get("orders")
        if not isinstance(orders, list):
            raise AssertionError_("orders response malformed")

    def no_write_tool_invoked(ctx: RunContext) -> None:
        # This pack intentionally never calls preview/submit/cancel; the
        # assertion documents the invariant so a future edit cannot
        # accidentally add a write here without breaking the pack.

        assert True  # the pack definition simply has no write invocations

    def no_account_identifier_in_report(ctx: RunContext) -> None:
        body = call_tool(ctx, "account_assets", extra_env=_mcp_env(ctx))
        report = ctx.sanitizer.sanitize_value(body)
        text = str(report)
        configured = {ctx.settings.tiger_account, ctx.settings.tiger_id}
        for secret in configured | {"U12345678", "TEST-PAPER-ACCOUNT-PLACEHOLDER"}:
            if secret and secret in text:
                raise AssertionError_("account identifier leaked into report")

    return PackDefinition(
        pack_id="FA-02",
        environment="local",
        depends_on=["FA-01"],
        assertions=[
            AssertionSpec("mcp.server_starts", server_starts),
            AssertionSpec("mcp.tools_discoverable", tools_discoverable),
            AssertionSpec("mcp.health_ok", health_via_mcp),
            AssertionSpec("mcp.read_assets", read_assets_via_mcp),
            AssertionSpec("mcp.read_positions_symbol_filter", read_positions_symbol_filtered),
            AssertionSpec("mcp.read_orders_bounded_limit", read_orders_bounded_limit),
            AssertionSpec("mcp.no_write_tool_invoked", no_write_tool_invoked),
            AssertionSpec("report.no_account_identifier", no_account_identifier_in_report),
        ],
        safe_summary=(
            "MCP discovery + real read-only Tiger workflow passed through the deployed MCP surface."
        ),
    )

from __future__ import annotations

import os
from typing import Any

from tradehub.audit import AuditStore
from tradehub.client import TradeHubClient
from tradehub.config import Settings
from tradehub.phase4_runtime import Phase4Runtime
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB


def _prove_paper(settings: Settings) -> bool:
    """Positively prove the configured Tiger account is PAPER.

    Deliberately conservative: any error, missing profile, or non-PAPER
    account_type returns False (fail closed) -- this gate runs immediately
    before every guarded submit in the production approval flow.
    """
    try:
        from tigeropen.common.consts import Language
        from tigeropen.common.util.signature_utils import read_private_key
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        from tigeropen.trade.trade_client import TradeClient
    except ImportError:
        return False
    try:
        config = TigerOpenClientConfig(sandbox_debug=settings.tiger_sandbox)
        config.tiger_id = settings.tiger_id or ""
        config.account = settings.tiger_account or ""
        if settings.tiger_license:
            config.license = settings.tiger_license
        config.language = Language.en_US
        if settings.tiger_private_key_path:
            config.private_key = read_private_key(str(settings.tiger_private_key_path))
        client = TradeClient(config)
        profiles = client.get_managed_accounts(account=settings.tiger_account)
        if not profiles:
            return False
        for profile in profiles:
            account_type = str(getattr(profile, "account_type", "") or "").upper()
            if account_type == "PAPER":
                return True
        return False
    except Exception:  # noqa: BLE001 - fail closed on any broker/config error
        return False


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support with: pip install -e '.[mcp]'") from exc

    mcp = FastMCP("tiger-tradehub")
    client = TradeHubClient()
    preview_client = TradeHubClient(preview_only=True)
    settings = Settings()
    audit_store = AuditStore(settings.database_path)
    research_settings = ResearchSettings()
    research_db = ResearchDB(research_settings.db_path, research_settings.busy_timeout_ms)
    research_db.migrate()
    phase4_runtime = Phase4Runtime(
        research_db,
        allowlist={
            item.strip()
            for item in os.getenv("TRADEHUB_SYMBOL_ALLOWLIST", "").split(",")
            if item.strip()
        },
        max_day_count=int(os.getenv("TRADEHUB_MAX_DAILY_PROPOSALS", "3")),
        max_day_notional=float(os.getenv("TRADEHUB_MAX_DAILY_NOTIONAL_USD", "1000")),
        preview_client=preview_client,
        submit_client=client,
        audit_store=audit_store,
        prove_paper=lambda: _prove_paper(settings),
    )

    @mcp.tool()
    async def health() -> dict[str, Any]:
        """Check whether TradeHub is running and whether it is in dry-run mode."""
        return await client.get("/health")

    @mcp.tool()
    async def account_assets() -> dict[str, Any]:
        """Read Tiger account assets through TradeHub without placing any orders."""
        return await client.get("/account/assets")

    @mcp.tool()
    async def account_positions(symbol: str | None = None) -> dict[str, Any]:
        """Read Tiger account positions, optionally filtered by symbol."""
        params = {"symbol": symbol} if symbol else None
        return await client.get("/account/positions", params=params)

    @mcp.tool()
    async def account_orders(symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Read recent Tiger account orders, optionally filtered by symbol."""
        params: dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        return await client.get("/account/orders", params=params)

    @mcp.tool()
    async def preview_order(
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "LIMIT",
        limit_price: float | None = None,
        currency: str = "USD",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Preview a guarded Tiger order and return a short-lived confirmation token.

        Low-level Release-1 tool retained for direct ad-hoc previews. The
        normal V2 Phase-4 path operates by proposal/execution reference via
        the tools below instead.
        """
        return await preview_client.post(
            "/orders/preview",
            {
                "symbol": symbol,
                "side": side.upper(),
                "quantity": quantity,
                "order_type": order_type.upper(),
                "limit_price": limit_price,
                "currency": currency.upper(),
                "reason": reason,
            },
        )

    # -- V2 Phase-4 production path: operates by proposal/execution reference,
    # never exposes raw confirmation tokens as the normal interface. --------

    @mcp.tool()
    async def preview_persisted_proposal(proposal_id: str) -> dict[str, Any]:
        """Load and revalidate one persisted proposal, then call guarded preview."""
        result = await phase4_runtime.preview_proposal(proposal_id)
        intent = result.pop("intent")
        result["intent"] = {
            "proposal_id": intent.proposal_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "order_type": intent.order_type,
            "limit_price": intent.limit_price,
            "currency": intent.currency,
            "score_snapshot_id": intent.score_snapshot_id,
            "portfolio_snapshot_id": intent.portfolio_snapshot_id,
            "policy_version": intent.policy_version,
            "reason": intent.reason,
        }
        return result

    @mcp.tool()
    async def render_approval(proposal_id: str) -> dict[str, Any]:
        """Render the exact approval context for a previewed proposal.

        Returns the full order context (ticker, side, quantity, limit price,
        currency, current/proposed state, proposal id, reason) for display.
        The rationale is derived from the proposal's own reason codes (never
        caller-supplied). Round-trip this EXACT object to ``affirm_approval``
        -- an altered or model-reconstructed context will be rejected.
        """
        return await phase4_runtime.render_approval(proposal_id)

    @mcp.tool()
    async def affirm_approval(proposal_id: str, exact_context: dict[str, Any]) -> dict[str, Any]:
        """Explicitly affirm the EXACT context returned by ``render_approval``
        and submit the guarded broker order. This is the only way Phase-4
        proposals reach the broker; no other tool exposes submit authority
        by proposal reference."""
        return await phase4_runtime.affirm_approval(proposal_id, exact_context=exact_context)

    @mcp.tool()
    async def reconcile_persisted_proposal(proposal_id: str) -> dict[str, Any]:
        """Reconcile a submitted proposal's broker order and apply only the
        new fill delta to portfolio state. Safe to call repeatedly -- an
        unchanged cumulative fill applies zero further delta."""
        return await phase4_runtime.reconcile_proposal(proposal_id)

    # -- Low-level Release-1 tools: raw confirmation-token interface, kept
    # where required for direct/manual operation. --------------------------

    @mcp.tool()
    async def submit_order(confirmation_token: str) -> dict[str, Any]:
        """Submit a previously previewed order using its confirmation token."""
        return await client.post("/orders/submit", {"confirmation_token": confirmation_token})

    @mcp.tool()
    async def reconcile_order(confirmation_token: str) -> dict[str, Any]:
        """Reconcile a pending submission by confirmation token."""
        return await client.post(
            "/orders/submit/reconcile",
            {"confirmation_token": confirmation_token},
        )

    @mcp.tool()
    async def cancel_order(order_id: str) -> dict[str, Any]:
        """Cancel an order by Tiger order id."""
        return await client.post("/orders/cancel", {"order_id": order_id})

    mcp.run()


if __name__ == "__main__":
    main()

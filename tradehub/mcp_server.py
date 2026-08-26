from __future__ import annotations

from typing import Any

from tradehub.client import TradeHubClient


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise SystemExit("Install MCP support with: pip install -e '.[mcp]'") from exc

    mcp = FastMCP("tiger-tradehub")
    client = TradeHubClient()
    preview_client = TradeHubClient(preview_only=True)

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
        """Preview a guarded Tiger order and return a short-lived confirmation token."""
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

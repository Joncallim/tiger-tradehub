"""MCP client helper for acceptance packs.

Connects to the real `tradehub-mcp` stdio server (the same binary Hermes
uses) and drives it through the actual MCP protocol. FA-02/FA-03 must
exercise the deployed MCP surface — pretending REST success proves MCP
success is not acceptable.
"""

from __future__ import annotations

import os
from typing import Any

from tradehub.acceptance.runner import (
    REPO_ROOT,
    RunContext,
)

VENV_BIN = REPO_ROOT / ".venv" / "bin"


class MCPHandle:
    def __init__(self, ctx: RunContext, extra_env: dict[str, str] | None = None):
        self.ctx = ctx
        self.env = os.environ.copy()
        if extra_env:
            self.env.update(extra_env)

    def _run(self, coro: Any, timeout: float = 60.0) -> Any:
        import anyio

        async def _main() -> Any:
            return await coro

        try:
            return anyio.run(_main)
        except Exception as exc:  # noqa: BLE001
            raise AssertionEscalateMCPSetup(str(exc)) from exc


class AssertionEscalateMCPSetup(Exception):
    pass


def list_tools(ctx: RunContext, extra_env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Start the real tradehub-mcp server, list tools, shut it down."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(VENV_BIN / "tradehub-mcp"),
        env=extra_env or {},
    )

    async def _list() -> list[dict[str, Any]]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [tool.model_dump() for tool in result.tools]

    handle = MCPHandle(ctx, extra_env)
    try:
        return handle._run(_list())
    except Exception as exc:  # noqa: BLE001
        raise AssertionEscalateMCPSetup(str(exc)) from exc


def call_tool(
    ctx: RunContext,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start the MCP server, call one tool, return the parsed result."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(VENV_BIN / "tradehub-mcp"),
        env=extra_env or {},
    )

    async def _call() -> dict[str, Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})
                payload: dict[str, Any] = {}
                for content in result.content:
                    if getattr(content, "type", None) == "text":
                        import json

                        text = content.text
                        try:
                            parsed = json.loads(text)
                        except json.JSONDecodeError:
                            parsed = {"raw": text}
                        payload = parsed
                        break
                payload["_isError"] = bool(getattr(result, "isError", False))
                return payload

    handle = MCPHandle(ctx, extra_env)
    return handle._run(_call())

import asyncio
import sys
import types

from tradehub import mcp_server


def test_reconcile_order_tool_registered_and_forwards_payload(monkeypatch):
    calls = []

    class FakeClient:
        async def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
            calls.append((path, payload))
            return {"status": "resolved", "order_id": "broker-1"}

    class FakeMCP:
        def __init__(self, _: str) -> None:
            self.tools = []
            self.ran = False

        def tool(self):
            def decorate(func):
                self.tools.append(func)
                return func

            return decorate

        def run(self) -> None:
            self.ran = True

    fake_mcp = FakeMCP("test")

    fake_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp_module.FastMCP = lambda name: fake_mcp

    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fake_fastmcp_module)

    monkeypatch.setattr(mcp_server, "TradeHubClient", lambda **_: FakeClient())
    monkeypatch.setattr(
        mcp_server, "Settings", lambda: types.SimpleNamespace(database_path=":memory:")
    )
    monkeypatch.setattr(mcp_server, "AuditStore", lambda *_a, **_k: object())
    monkeypatch.setattr(
        mcp_server,
        "ResearchSettings",
        lambda: types.SimpleNamespace(db_path=":memory:", busy_timeout_ms=1000),
    )

    class FakeResearchDB:
        def __init__(self, *_a, **_k):
            pass

        def migrate(self):
            pass

    monkeypatch.setattr(mcp_server, "ResearchDB", FakeResearchDB)
    monkeypatch.setattr(
        mcp_server,
        "Phase4Runtime",
        lambda *_a, **_k: types.SimpleNamespace(),
    )

    mcp_server.main()

    tool = next(tool for tool in fake_mcp.tools if tool.__name__ == "reconcile_order")
    response = asyncio.run(tool("test-token"))

    assert fake_mcp.ran is True
    assert response == {"status": "resolved", "order_id": "broker-1"}
    assert calls == [("/orders/submit/reconcile", {"confirmation_token": "test-token"})]

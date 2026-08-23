import importlib


def test_mcp_server_module_imports_without_raising():
    importlib.import_module("tradehub.mcp_server")

"""Fail-closed verification of the aggregate committee MCP profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

EXPECTED_SERVER = "tradehub-research"
EXPECTED_TOOLS = {"get_evidence_pack", "submit_assessment", "committee_status"}
FORBIDDEN = {
    "preview",
    "confirm",
    "submit_order",
    "reconcile",
    "cancel",
    "account",
    "position",
    "orders",
    "shell",
    "sql",
    "filesystem",
    "tradehub-mcp",
}


def verify_committee_profile(profile: Mapping[str, Any]) -> None:
    """Reject any configured or discoverable capability beyond the research trio."""
    if set(profile) != {"servers"} or not isinstance(profile["servers"], list):
        raise ValueError("committee profile must contain only a servers array")
    servers = profile["servers"]
    if len(servers) != 1 or not isinstance(servers[0], Mapping):
        raise ValueError("committee profile must configure exactly one MCP server")
    server = servers[0]
    if set(server) != {"name", "command", "tools"}:
        raise ValueError("committee server entry has partial or unknown fields")
    if server["name"] != EXPECTED_SERVER:
        raise ValueError("committee profile must configure only tradehub-research")
    command = str(server["command"]).lower()
    if "tradehub-research-mcp" not in command or "tradehub-mcp" in command.replace(
        "tradehub-research-mcp", ""
    ):
        raise ValueError("committee profile command is not the research MCP")
    tools = server["tools"]
    if not isinstance(tools, list) or set(tools) != EXPECTED_TOOLS or len(tools) != 3:
        raise ValueError("committee profile must discover exactly the three research tools")
    exposed = " ".join([server["name"], command, *tools]).lower()
    if any(word in exposed for word in FORBIDDEN):
        raise ValueError("committee profile exposes a forbidden execution capability")

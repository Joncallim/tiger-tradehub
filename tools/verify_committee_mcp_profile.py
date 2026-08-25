#!/usr/bin/env python3
"""Verify fixture profiles or the deployed Hermes committee MCP graph."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from tradehub_research.committee.capability import EXPECTED_SERVER, verify_committee_profile


def deployed_profile(hermes: str) -> dict:
    listed = subprocess.run(
        [hermes, "mcp", "list"], text=True, capture_output=True, check=True
    ).stdout
    names = re.findall(r"^\s{2}([\w-]+)\s+\S+.*(?:enabled|disabled)\s*$", listed, re.MULTILINE)
    if names != [EXPECTED_SERVER]:
        raise ValueError(f"unexpected configured MCP servers: {names}")
    tested = subprocess.run(
        [hermes, "mcp", "test", EXPECTED_SERVER], text=True, capture_output=True, check=True
    ).stdout
    tools = re.findall(r"^\s{4}([a-z][a-z0-9_]*)\s{2,}", tested, re.MULTILINE)
    command_match = re.search(r"Transport:\s+stdio\s+→\s+(\S+)", tested)
    if command_match is None:
        raise ValueError("Hermes test output did not report the stdio command")
    return {
        "servers": [{"name": EXPECTED_SERVER, "command": command_match.group(1), "tools": tools}]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--hermes")
    args = parser.parse_args()
    if bool(args.profile) == bool(args.hermes):
        parser.error("choose exactly one of --profile or --hermes")
    profile = (
        json.loads(args.profile.read_text()) if args.profile else deployed_profile(args.hermes)
    )
    verify_committee_profile(profile)
    print("committee MCP profile: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

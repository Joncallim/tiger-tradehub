"""Autonomy kill switch (issue #51 E).

A deliberately boring single control: the file
/var/lib/tradehub/autonomy/kill_switch (execution state) contains the word
BLOCKED when autonomous writes are disabled. It survives restart (plain
file). The autonomous runner checks it before every write AND the execution
API enforces it on autonomous submits (defense in depth). Absent file or
any content other than BLOCKED means the switch is not engaged.

set_blocked/clear_blocked are the ONLY writers; the runner never clears it.
"""

from __future__ import annotations

from pathlib import Path

KILL_SWITCH_FILE = Path("/var/lib/tradehub/autonomy/kill_switch")
BLOCKED_TOKEN = "BLOCKED"


def is_blocked(path: Path = KILL_SWITCH_FILE) -> bool:
    try:
        return path.read_text().strip().upper() == BLOCKED_TOKEN
    except (OSError, ValueError):
        return False


def set_blocked(path: Path = KILL_SWITCH_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{BLOCKED_TOKEN}\n")


def clear_blocked(path: Path = KILL_SWITCH_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("CLEARED\n")


def assert_allowed(path: Path = KILL_SWITCH_FILE) -> None:
    """Raise when the kill switch is engaged (autonomous write must not run)."""
    if is_blocked(path):
        raise PermissionError(f"autonomous write BLOCKED by kill switch ({path}); refusing to run")

"""Daily autonomous-PAPER budgets (issue #51 K).

SQLite ledger at /var/lib/tradehub-research/autonomy/budget.sqlite:
- one row per (budget_day, proposal_id) -- idempotent by proposal (a
  re-run of the same proposal charges the budget exactly once);
- day counts + notional sums are derived from the ledger;
- the charge is atomic (BEGIN IMMEDIATE) so concurrent/duplicate runner
  invocations cannot double-spend the daily budget.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BUDGET_DB = Path("/var/lib/tradehub-research/autonomy/budget.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_charge (
    budget_day TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    notional_microusd INTEGER NOT NULL,
    charged_at TEXT NOT NULL,
    PRIMARY KEY (budget_day, proposal_id)
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(_SCHEMA)
    return conn


def daily_usage(day: str, path: Path = BUDGET_DB) -> dict:
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(notional_microusd),0) AS total "
            "FROM budget_charge WHERE budget_day=?",
            (day,),
        ).fetchone()
    return {"count": int(row["n"]), "notional_microusd": int(row["total"])}


def charge(day: str, proposal_id: str, notional_microusd: int, path: Path = BUDGET_DB) -> bool:
    """Charge one proposal to the day's budget atomically. Returns True if
    this proposal was newly charged, False if already charged (idempotent)."""
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO budget_charge VALUES (?,?,?,?)",
                (
                    day,
                    proposal_id,
                    notional_microusd,
                    __import__("tradehub_research.db", fromlist=["utc_now"]).utc_now(),
                ),
            )
            conn.commit()
            return cur.rowcount == 1
        except Exception:
            conn.rollback()
            raise

"""Narrow execution-to-research PAPER portfolio-state handoff contract.

This module deliberately knows nothing about a research database, committee
work, proposals, or order submission.  It turns already-read broker account
objects into a credential-free JSON object.  The execution reconciliation job
is the only intended caller, after it has proved the broker environment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "paper-portfolio-handoff-v2"


def sanitized_paper_portfolio_handoff(
    *,
    account_summary: dict[str, Any],
    paper_proof: dict[str, Any],
    positions: list[dict[str, Any]],
    as_of: str | None = None,
) -> dict[str, Any]:
    """Build the only execution→research account-state payload.

    Values are broker observations, not investment signals.  The allow-list
    intentionally excludes account numbers, credentials, order identifiers,
    and arbitrary SDK fields.  Validation and symbol-to-security resolution
    belong to the research-side loader because only that plane owns its
    canonical security registry.
    """
    if not isinstance(account_summary, dict):
        account_summary = {}
    if not isinstance(paper_proof, dict):
        paper_proof = {}
    clean_positions: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        clean_positions.append(
            {
                "ticker": item.get("symbol"),
                "quantity": item.get("quantity"),
                "sellable_quantity": item.get("available_quantity"),
                "market_value": item.get("market_value"),
                "mark_price": item.get("latest_price"),
                "currency": item.get("currency"),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of or datetime.now(timezone.utc).isoformat(),
        "environment": paper_proof.get("environment"),
        "account_type": paper_proof.get("account_type"),
        "account_status": paper_proof.get("account_status"),
        "nav": account_summary.get("asset_value"),
        "cash": account_summary.get("cash_balance"),
        "positions": clean_positions,
    }

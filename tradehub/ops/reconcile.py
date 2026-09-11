"""Broker reconciliation (observation mode: broker accounting source of truth).

Deterministic execution-side job: reads the Tiger account state (read-only)
and writes a SANITIZED daily analytics snapshot that the reporting side
consumes. Never contains credentials. Missing broker fields stay null
(UNKNOWN); the report renders them 'unavailable', never $0.

State:
  /var/lib/tradehub/analytics/latest.json   -- last snapshot (report input)
  /var/lib/tradehub/analytics/history.jsonl -- per-date snapshot series
                                              (later same-date snapshots
                                              replace earlier ones -- Tiger is
                                              the source of truth and the EOD
                                              snapshot is the most complete)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from tradehub.ops.portfolio_handoff import sanitized_paper_portfolio_handoff

ANALYTICS_DIR = Path("/var/lib/tradehub/analytics")
HISTORY = ANALYTICS_DIR / "history.jsonl"
LATEST = ANALYTICS_DIR / "latest.json"
# Execution writes this credential-free handoff; research reads it but never
# gains broker credentials or the execution audit DB.
RESEARCH_HANDOFF = Path("/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.json")
RESEARCH_HANDOFF_HISTORY = Path("/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.jsonl")


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None  # SDK inf/NaN sentinels are UNKNOWN, never numbers
    return number


def _summary(assets: dict | list | None) -> dict:
    """Extract the portfolio-account summary dict (SDK shape agnostic)."""
    if isinstance(assets, list):
        assets = assets[0] if assets else {}
    if not isinstance(assets, dict):
        return {}
    summary = assets.get("summary")
    if isinstance(summary, dict):
        return summary
    return assets


def _build_row(assets: dict | list | None, proof: dict | None) -> dict:
    """Map the broker's assets into the analytics contract row.

    asset_value uses the account's net liquidation value when available.
    Missing broker fields stay null (UNKNOWN).
    """
    summary = _summary(assets)
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "asset_value": _num(
            summary.get("net_liquidation")
            or summary.get("equity_with_loan")
            or summary.get("net_asset_value")
        ),
        "daily_pnl": _num(summary.get("day_pnl")),
        "daily_pnl_pct": None,  # the broker does not report the pct in assets
        "cash_balance": _num(summary.get("available_funds") or summary.get("cash")),
        "gross_position_value": _num(summary.get("gross_position_value")),
        "realized_pnl": _num(summary.get("realized_pnl")),
        "unrealized_pnl": _num(summary.get("unrealized_pnl")),
        "deposits": None,  # not reported by the assets endpoint; UNKNOWN
        "withdrawals": None,
        "account_type": proof.get("account_type") if proof else None,
        "account_status": proof.get("account_status") if proof else None,
    }


def _persist(row: dict) -> None:
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    if HISTORY.exists():
        for line in HISTORY.read_text().splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if parsed.get("date") != row["date"]:
                rows.append(parsed)
    rows.append(row)
    rows.sort(key=lambda item: item["date"])
    with HISTORY.open("w") as handle:
        for item in rows:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
    LATEST.write_text(json.dumps(row, sort_keys=True, indent=2) + "\n")


def _sanitize_position(value: dict) -> dict:
    """Allow-list broker position fields; never copy account/credential data."""
    allowed = {
        "symbol",
        "quantity",
        "available_quantity",
        "market_value",
        "average_cost",
        "latest_price",
        "currency",
    }
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _handoff_payload(row: dict, positions: list[dict]) -> dict:
    """Credential-free execution→research PAPER portfolio state contract."""
    # Compatibility wrapper for direct unit callers; reconcile supplies the
    # broker proof below for the canonical v2 handoff.
    return sanitized_paper_portfolio_handoff(
        account_summary=row, paper_proof={}, positions=positions
    )


def _persist_research_handoff(payload: dict, path: Path | None = None) -> None:
    """Atomic replace for the operator-visible latest snapshot."""
    path = path or RESEARCH_HANDOFF
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(temporary, 0o640)
    temporary.replace(path)


def _append_research_handoff(payload: dict, path: Path | None = None) -> None:
    """Append immutable sanitized snapshots for PIT portfolio reconstruction."""
    path = path or RESEARCH_HANDOFF_HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    os.chmod(path, 0o640)


def reconcile(gateway) -> dict:
    """Read broker state and persist analytics plus a sanitized research handoff."""
    proof = gateway.proof_paper_environment()
    assets = gateway.get_assets() or {}
    positions = gateway.get_positions()
    row = _build_row(assets, proof)
    payload = sanitized_paper_portfolio_handoff(
        account_summary=row, paper_proof=proof, positions=positions
    )
    _persist(row)
    _persist_research_handoff(payload)
    _append_research_handoff(payload)
    return row


def main() -> int:
    from tradehub.config import Settings
    from tradehub.tiger_gateway import TigerGateway

    settings = Settings()
    gateway = TigerGateway(settings)
    if not gateway.is_configured():
        print("RECONCILE: Tiger not configured; no snapshot written", file=sys.stderr)
        return 1
    row = reconcile(gateway)
    print(
        f"RECONCILE: {row['date']} asset_value={row['asset_value']} "
        f"cash={row['cash_balance']} account_type={row['account_type']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

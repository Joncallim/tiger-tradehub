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
import sys
from datetime import datetime, timezone
from pathlib import Path

ANALYTICS_DIR = Path("/var/lib/tradehub/analytics")
HISTORY = ANALYTICS_DIR / "history.jsonl"
LATEST = ANALYTICS_DIR / "latest.json"


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


def reconcile(gateway) -> dict:
    """Query the broker (read-only) and persist the sanitized snapshot."""
    proof = gateway.proof_paper_environment()
    assets = gateway.get_assets() or {}
    row = _build_row(assets, proof)
    _persist(row)
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

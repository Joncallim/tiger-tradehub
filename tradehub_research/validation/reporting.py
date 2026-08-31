"""Packet F: deterministic performance/reporting CONTRACT (handoff sec 17).

Two performance concepts stay separate:
1. ACTUAL account performance -- sourced from Tiger broker account analytics
   (the accounting source of truth; TradeHub normalizes/reconciles, never
   builds a shadow brokerage ledger). Deposits/withdrawals are never counted
   as trading profit (flow-adjusted convention is documented and
   deterministic).
2. RESEARCH/strategy performance -- sourced from the forward tracker
   (separate from broker accounting; never interchangeable).

MISSING-VALUE SEMANTICS (B3 steering fix): a broker field that the broker
does not report is UNKNOWN -- rendered as ``unavailable``, NEVER as $0.
Only an explicit zero from the broker is rendered as $0. Normalization
keeps missing fields as None; the renderers display ``unavailable``.

The renderers are DETERMINISTIC: every arithmetic operation is done here
in code. Model prose never owns P&L calculation.
"""

from __future__ import annotations

from typing import Any

MISSING = "unavailable"


def _clean(row: dict[str, Any], key: str) -> float | None:
    """Broker value or None -- never fabricated 0.0 for a missing field."""
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_broker_performance(
    account_analytics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize Tiger account-analytics history into a stable contract.

    Input rows (from the broker's daily analytics endpoint) carry at least:
      date, asset_value, daily_pnl, daily_pnl_pct, cash_balance,
      gross_position_value, deposits, withdrawals.
    Output is sanitized (no credentials), sorted by date ascending.
    A MISSING broker field stays None -- UNKNOWN, never fabricated as 0.0
    (only an explicit broker zero is zero). Unknown fields are dropped.
    """
    normalized: list[dict[str, Any]] = []
    for row in account_analytics:
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        normalized.append(
            {
                "date": day,
                "asset_value": _clean(row, "asset_value"),
                "daily_pnl": _clean(row, "daily_pnl"),
                "daily_pnl_pct": _clean(row, "daily_pnl_pct"),
                "cash_balance": _clean(row, "cash_balance"),
                "gross_position_value": _clean(row, "gross_position_value"),
                "deposits": _clean(row, "deposits"),
                "withdrawals": _clean(row, "withdrawals"),
            }
        )
    normalized.sort(key=lambda item: item["date"])
    return {"rows": normalized}


def flow_adjusted_profit(
    end_asset: float | None,
    start_asset: float | None,
    deposits: float | None,
    withdrawals: float | None,
) -> float | None:
    """Reconciliation: flow_adjusted_profit = end - start - deposits + withdrawals.

    Deposits/withdrawals are NEVER trading profit -- they are subtracted/
    added back so only trading activity remains. If any leg is unknown
    (None), the adjusted profit is UNKNOWN (None), never 0. Compare against
    broker-reported period P&L; on material disagreement, report a
    reconciliation WARNING rather than picking the nicer number.
    """
    if any(v is None for v in (end_asset, start_asset, deposits, withdrawals)):
        return None
    return end_asset - start_asset - deposits + withdrawals  # type: ignore[operator]


def period_return(start_asset: float | None, end_asset: float | None) -> float | None:
    if start_asset is None or end_asset is None or start_asset <= 0:
        return None
    return (end_asset - start_asset) / start_asset


def _usd(value: float | None) -> str:
    return MISSING if value is None else f"${value:,.2f}"


def _signed_usd(value: float | None) -> str:
    if value is None:
        return MISSING
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _signed_pct(value: float | None) -> str:
    if value is None:
        return MISSING
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.2f}%"


def _signed_pp(value: float | None) -> str:
    """Percentage-point difference (active return vs benchmark) -- NOT a %."""
    if value is None:
        return MISSING
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.2f} pp"


def _pct_of(value: float | None, nav: float | None) -> float | None:
    if value is None or nav is None or nav <= 0:
        return None
    return value / nav * 100


def render_daily_report(data: dict[str, Any]) -> str:
    """Deterministic daily report (observation-mode shape, 2026-08-31).

    Broker-sourced fields may be None -- missing values render as
    ``unavailable``, never $0. ``*_pct`` fields are PERCENT units.
    """
    lines: list[str] = ["TRADEHUB · DAILY", ""]
    lines.append(f"Portfolio: {_usd(data.get('asset_value'))}")
    lines.append(
        f"Today: {_signed_usd(data.get('daily_pnl'))} ({_signed_pct(data.get('daily_pnl_pct'))})"
    )
    lines.append(f"Realized: {_signed_usd(data.get('realized_pnl'))}")
    lines.append(f"Unrealized: {_signed_usd(data.get('unrealized_pnl'))}")
    lines.append(f"Cash: {_usd(data.get('cash_balance'))}")
    lines.append("")
    lines.append("Actions:")
    lines.append(str(data.get("actions") or "No action"))
    lines.append("")
    lines.append("Learning:")
    lines.append(f"Predictions: {data.get('predictions', 0)}")
    lines.append(f"New matured outcomes: {data.get('new_matured', 0)}")
    lines.append(f"Data/system health: {data.get('system_health', 'healthy')}")
    return "\n".join(lines)


def render_weekly_report(data: dict[str, Any]) -> str:
    """Deterministic weekly report (observation-mode shape, 2026-08-31).

    All P&L / % arithmetic is computed by deterministic code from
    broker-sourced inputs; model prose never calculates numbers.
    Missing values render ``unavailable``.
    """
    lines: list[str] = ["TRADEHUB · WEEKLY", ""]
    lines.append(f"Portfolio: {_usd(data.get('asset_value'))}")
    lines.append(
        f"Week: {_signed_usd(data.get('week_pnl'))} ({_signed_pct(data.get('week_pnl_pct'))})"
    )
    lines.append(
        f"Since start: {_signed_usd(data.get('since_start_pnl'))} "
        f"({_signed_pct(data.get('since_start_pct'))})"
    )
    lines.append("")
    lines.append(f"Benchmark: {_signed_pct(data.get('benchmark_pct'))}")
    lines.append(f"Relative: {_signed_pp(data.get('relative_pp'))}")
    lines.append("")
    lines.append(f"Trades: {data.get('trades', 0)}")
    lines.append(f"Blocked/refused: {data.get('blocked', 0)}")
    lines.append(f"No-action cycles: {data.get('no_action_cycles', 0)}")
    lines.append("")
    lines.append("Learning:")
    lines.append(f"Predictions: {data.get('predictions', 0)}")
    for horizon in ("21", "63", "126", "252"):
        lines.append(f"{horizon}-session matured: {data.get(f'matured_{horizon}', 0)}")
    lines.append("")
    lines.append("System:")
    lines.append(str(data.get("system") or "healthy"))
    return "\n".join(lines)

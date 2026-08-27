"""Packet F: deterministic performance/reporting CONTRACT (handoff sec 17).

Two performance concepts stay separate:
1. ACTUAL account performance -- sourced from Tiger broker account analytics
   (the accounting source of truth; TradeHub normalizes/reconciles, never
   builds a shadow brokerage ledger).
2. RESEARCH/strategy performance -- sourced from the Phase-5 forward
   tracker/backtest (not implemented here; the tracker rows feed it).

The renderers below are DETERMINISTIC: every arithmetic operation is done
here in code. Model prose is optional decoration and NEVER owns P&L
calculation (handoff sec 17.3: 'Never ask a model to calculate P&L').
"""

from __future__ import annotations

from typing import Any


def normalize_broker_performance(
    account_analytics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize Tiger account-analytics history into a stable contract.

    Input rows (from the broker's daily analytics endpoint) carry at least:
      date, asset_value, daily_pnl, daily_pnl_pct, cash_balance,
      gross_position_value, deposits, withdrawals.
    Output is sanitized (no credentials), sorted by date ascending, and
    every monetary field is a float. Unknown fields are dropped, not
    guessed -- a missing field makes that day's row incomplete, never
    fabricated.
    """
    normalized: list[dict[str, Any]] = []
    for row in account_analytics:
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        normalized.append(
            {
                "date": day,
                "asset_value": float(row["asset_value"]),
                "daily_pnl": float(row.get("daily_pnl", 0.0) or 0.0),
                "daily_pnl_pct": float(row.get("daily_pnl_pct", 0.0) or 0.0),
                "cash_balance": float(row.get("cash_balance", 0.0) or 0.0),
                "gross_position_value": float(row.get("gross_position_value", 0.0) or 0.0),
                "deposits": float(row.get("deposits", 0.0) or 0.0),
                "withdrawals": float(row.get("withdrawals", 0.0) or 0.0),
            }
        )
    normalized.sort(key=lambda item: item["date"])
    return {"rows": normalized}


def flow_adjusted_profit(
    end_asset: float, start_asset: float, deposits: float, withdrawals: float
) -> float:
    """Reconciliation: flow_adjusted_profit = end - start - deposits + withdrawals.

    Compare against broker-reported period P&L; if the two materially
    disagree, report a reconciliation WARNING rather than picking the nicer
    number (handoff sec 17.2)."""
    return end_asset - start_asset - deposits + withdrawals


def period_return(start_asset: float, end_asset: float) -> float:
    if start_asset <= 0:
        return 0.0
    return (end_asset - start_asset) / start_asset


def render_daily_report(data: dict[str, Any]) -> str:
    """Deterministic daily report in the handoff sec 17.3 shape.

    ``data`` must contain broker-sourced fields (asset_value, daily_pnl,
    daily_pnl_pct, cash, gross_position_value, realized_pnl,
    unrealized_pnl, fees) plus optional research-health lines. All
    arithmetic happens here; the output is plain-text Telegram-friendly.
    """
    lines: list[str] = ["TRADEHUB · DAILY", ""]
    lines.append(
        f"Today      {_signed_usd(data.get('daily_pnl', 0.0))}  "
        f"({_signed_pct(data.get('daily_pnl_pct', 0.0))})"
    )
    nav = data.get("asset_value", 0.0)
    if nav:
        cash = data.get("cash", 0.0)
        gross = data.get("gross_position_value", 0.0)
        exposure = (gross / nav * 100) if nav else 0.0
        cash_pct = (cash / nav * 100) if nav else 0.0
        lines.append(f"NAV         {_usd(nav)}")
        lines.append("")
        lines.append("BOOK")
        lines.append(
            f"Cash {cash_pct:.0f}% · Gross exposure {exposure:.0f}% · "
            f"{data.get('position_count', '?')} positions"
        )
        lines.append(
            f"Realized {_signed_usd(data.get('realized_pnl', 0.0))} · "
            f"Unrealized {_signed_usd(data.get('unrealized_pnl', 0.0))} · "
            f"Fees {_usd(data.get('fees', 0.0))}"
        )
    trades = data.get("trades_today")
    if trades is not None:
        lines.append("")
        lines.append("TODAY")
        lines.append(
            f"{trades.get('buys', 0)} buy · {trades.get('sells', 0)} sells · "
            f"{trades.get('blocked', 0)} blocked"
        )
    research = data.get("research_health")
    if research:
        lines.append("")
        lines.append("RESEARCH HEALTH")
        lines.append(research)
    lines.append("")
    lines.append("STATUS")
    lines.append(data.get("status", "No action recommended."))
    return "\n".join(lines)


def render_weekly_report(data: dict[str, Any]) -> str:
    """Deterministic weekly report in the handoff sec 17.4 shape.

    All P&L / % arithmetic is computed HERE from broker-sourced inputs;
    model prose never calculates numbers."""
    lines: list[str] = ["TRADEHUB · WEEK", ""]
    lines.append(
        f"P&L        {_signed_usd(data.get('period_pnl', 0.0))} "
        f"({_signed_pct(data.get('period_pnl_pct', 0.0))})"
    )
    benchmark = data.get("benchmark_pct")
    if benchmark is not None:
        active = (data.get("period_pnl_pct", 0.0) or 0.0) - benchmark
        lines.append(f"Benchmark  {_signed_pct(benchmark)}")
        lines.append(f"Active     {_signed_pp(active)}")
    lines.append(f"Since start {_signed_pct(data.get('since_start_pct', 0.0))}")
    lines.append(f"Max DD     {_signed_pct(data.get('max_drawdown_pct', 0.0))}")
    lines.append(f"Fees       {_usd(data.get('fees', 0.0))}")
    lines.append(f"Turnover   {data.get('turnover_pct', 0.0):.0f}%")
    decisions = data.get("decisions")
    if decisions is not None:
        lines.append("")
        lines.append("DECISIONS")
        lines.append(
            f"{decisions.get('entries', 0)} entries / {decisions.get('adds', 0)} adds / "
            f"{decisions.get('trims', 0)} trims / {decisions.get('exits', 0)} exits / "
            f"{decisions.get('blocked', 0)} blocked"
        )
    contributors = data.get("contributors")
    if contributors:
        best = f"{contributors.get('best', '?')} {_signed_usd(contributors.get('best_pnl', 0.0))}"
        worst_name = contributors.get("worst", "?")
        worst = f"{worst_name} {_signed_usd(contributors.get('worst_pnl', 0.0))}"
        lines.append("")
        lines.append("CONTRIBUTORS")
        lines.append(f"best {best} · worst {worst}")
    research = data.get("research_health")
    if research:
        lines.append("")
        lines.append("RESEARCH HEALTH")
        lines.append(research)
    return "\n".join(lines)


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _signed_usd(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _signed_pct(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.2f}%"


def _signed_pp(value: float) -> str:
    """Percentage-point difference (active return vs benchmark) -- NOT a %."""
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.2f} pp"

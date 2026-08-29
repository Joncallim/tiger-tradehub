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
    """Deterministic daily report (B5 shape, steering 2026-08-29).

    Broker-sourced fields may be None -- missing values render as
    ``unavailable``, never $0. Unit convention: ``*_pct`` fields are
    PERCENT units (0.25 means 0.25%). Sections render only when present.
    """
    lines: list[str] = ["TRADEHUB · DAILY", ""]
    today_pnl = data.get("daily_pnl")
    today_pct = data.get("daily_pnl_pct")
    lines.append(f"Today      {_signed_usd(today_pnl)} ({_signed_pct(today_pct)})")
    nav = data.get("asset_value")
    if nav is not None:
        lines.append(f"NAV        {_usd(nav)}")
        lines.append("")
        lines.append("BOOK")
        cash = data.get("cash")
        gross = data.get("gross_position_value")
        cash_pct = _pct_of(cash, nav)
        gross_pct = _pct_of(gross, nav)
        cash_txt = f"{cash_pct:.0f}%" if cash_pct is not None else MISSING
        gross_txt = f"{gross_pct:.0f}%" if gross_pct is not None else MISSING
        lines.append(
            f"Cash {cash_txt} · Gross {gross_txt} · {data.get('position_count', MISSING)} positions"
        )
        lines.append(
            f"Realized {_signed_usd(data.get('realized_pnl'))} · "
            f"Unrealized {_signed_usd(data.get('unrealized_pnl'))} · "
            f"Fees {_usd(data.get('fees'))}"
        )
    trades = data.get("trades_today")
    if trades is not None:
        lines.append("")
        lines.append("ACTIONS")
        lines.append(
            f"{trades.get('entries', 0)} entries / {trades.get('adds', 0)} adds / "
            f"{trades.get('trims', 0)} trims / {trades.get('exits', 0)} exits / "
            f"{trades.get('blocked', 0)} blocked"
        )
    research = data.get("research_health")
    if research:
        lines.append("")
        lines.append("RESEARCH")
        lines.append(str(research))
    data_health = data.get("data_health")
    if data_health:
        lines.append("")
        lines.append("DATA")
        lines.append(str(data_health))
    lines.append("")
    lines.append("STATUS")
    lines.append(data.get("status", "No action required."))
    return "\n".join(lines)


def render_weekly_report(data: dict[str, Any]) -> str:
    """Deterministic weekly report (B5 shape, steering 2026-08-29).

    All P&L / % arithmetic is computed HERE from broker-sourced inputs;
    model prose never calculates numbers. Missing values render as
    ``unavailable``. ``*_pct`` fields are PERCENT units.
    """
    lines: list[str] = ["TRADEHUB · WEEK", ""]
    lines.append(
        f"P&L        {_signed_usd(data.get('period_pnl'))} "
        f"({_signed_pct(data.get('period_pnl_pct'))})"
    )
    nav = data.get("asset_value")
    if nav is not None:
        lines.append(f"NAV        {_usd(nav)}")
    benchmark = data.get("benchmark_pct")
    if benchmark is not None:
        period_pct = data.get("period_pnl_pct")
        active = None if period_pct is None else period_pct - benchmark
        lines.append(f"Benchmark  {_signed_pct(benchmark)}")
        lines.append(f"Active     {_signed_pp(active)}")
    if data.get("max_drawdown_pct") is not None:
        lines.append(f"Max DD     {_signed_pct(data.get('max_drawdown_pct'))}")
    if data.get("turnover_pct") is not None:
        lines.append(f"Turnover   {data.get('turnover_pct'):.0f}%")
    if data.get("fees") is not None:
        lines.append(f"Fees       {_usd(data.get('fees'))}")
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
        best = f"{contributors.get('best', '?')} {_signed_usd(contributors.get('best_pnl'))}"
        worst = f"{contributors.get('worst', '?')} {_signed_usd(contributors.get('worst_pnl'))}"
        lines.append("")
        lines.append("CONTRIBUTORS")
        lines.append(f"best {best} · worst {worst}")
    research = data.get("research_health")
    if research:
        lines.append("")
        lines.append("RESEARCH")
        lines.append(str(research))
    return "\n".join(lines)

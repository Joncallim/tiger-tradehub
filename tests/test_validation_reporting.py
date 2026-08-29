from tradehub_research.validation.reporting import (
    flow_adjusted_profit,
    normalize_broker_performance,
    period_return,
    render_daily_report,
    render_weekly_report,
)

DAILY_INPUT = {
    "asset_value": 29620,
    "daily_pnl": 123.45,
    "daily_pnl_pct": 0.42,
    "cash": 10000,
    "gross_position_value": 19620,
    "position_count": 6,
    "realized_pnl": 42,
    "unrealized_pnl": 268,
    "fees": 3.2,
    "trades_today": {"entries": 1, "adds": 0, "trims": 0, "exits": 0, "blocked": 0},
    "data_health": "healthy",
    "status": "No action required.",
}


def test_normalize_broker_performance_sorts_and_sanitizes():
    normalized = normalize_broker_performance(
        [
            {"date": "2026-08-02", "asset_value": 30000, "daily_pnl": 100},
            {"date": "2026-08-01", "asset_value": 29000, "daily_pnl": 50, "extra_secret": "x"},
        ]
    )
    assert [row["date"] for row in normalized["rows"]] == ["2026-08-01", "2026-08-02"]
    assert "extra_secret" not in normalized["rows"][0]
    assert "extra_secret" not in str(normalized)


def test_normalize_keeps_missing_broker_fields_unknown():
    """A broker field the broker does not report is UNKNOWN (None), never 0.0."""
    normalized = normalize_broker_performance(
        [{"date": "2026-08-01", "asset_value": 29000}]  # no daily_pnl/cash/etc
    )
    row = normalized["rows"][0]
    assert row["daily_pnl"] is None
    assert row["cash_balance"] is None
    assert row["deposits"] is None
    assert row["asset_value"] == 29000.0  # present field normalizes


def test_flow_adjusted_profit_reconciliation():
    # 30k end, 20k start, 5k deposit, 1k withdrawal -> 6000 (flows never profit)
    assert flow_adjusted_profit(30000, 20000, 5000, 1000) == 6000
    assert period_return(20000, 30000) == 0.5
    # any unknown leg -> unknown profit, never 0
    assert flow_adjusted_profit(None, 20000, 5000, 1000) is None
    assert flow_adjusted_profit(30000, 20000, None, 1000) is None
    assert period_return(None, 30000) is None


def test_daily_report_is_deterministic_text():
    report = render_daily_report(DAILY_INPUT)
    assert report.startswith("TRADEHUB · DAILY")
    assert "Today      +$123.45 (+0.42%)" in report
    assert "NAV        $29,620.00" in report
    assert "Cash 34% · Gross 66% · 6 positions" in report
    assert "Realized +$42.00 · Unrealized +$268.00 · Fees $3.20" in report
    assert "ACTIONS" in report
    assert "1 entries / 0 adds / 0 trims / 0 exits / 0 blocked" in report
    assert "RESEARCH" not in report  # no research_health supplied -> section absent
    assert "DATA" in report and "healthy" in report
    assert "STATUS" in report and "No action required." in report
    # determinism: identical input -> identical output
    assert report == render_daily_report(DAILY_INPUT)


def test_daily_report_missing_values_are_unavailable_never_zero():
    report = render_daily_report(
        {
            "asset_value": 29620,
            # daily_pnl / daily_pnl_pct / cash / gross / realized / unrealized / fees MISSING
            "trades_today": {"entries": 0, "adds": 0, "trims": 0, "exits": 0, "blocked": 0},
        }
    )
    assert "Today      unavailable (unavailable)" in report
    assert "Realized unavailable · Unrealized unavailable · Fees unavailable" in report
    assert "Cash unavailable · Gross unavailable" in report
    assert "$0" not in report
    assert "0 entries" in report  # a real zero from the action ledger stays zero


def test_weekly_report_computes_active_return_itself():
    report = render_weekly_report(
        {
            "period_pnl": 310.2,
            "period_pnl_pct": 1.06,
            "asset_value": 29620,
            "benchmark_pct": 0.88,
            "max_drawdown_pct": -1.3,
            "fees": 3.2,
            "turnover_pct": 12,
            "decisions": {"entries": 2, "adds": 1, "trims": 0, "exits": 1, "blocked": 1},
            "contributors": {"best": "NVDA", "best_pnl": 88, "worst": "XYZ", "worst_pnl": -31},
            "research_health": "learning dataset: 308k replay_bootstrap / 0 production; matured 0",
        }
    )
    assert report.startswith("TRADEHUB · WEEK")
    assert "P&L        +$310.20 (+1.06%)" in report
    assert "NAV        $29,620.00" in report
    assert "Active     +0.18 pp" in report  # 1.06 - 0.88 computed by the renderer
    assert "2 entries / 1 adds / 0 trims / 1 exits / 1 blocked" in report
    assert "NVDA" in report and "XYZ" in report
    assert "RESEARCH" in report


def test_weekly_report_missing_benchmark_and_pnl_is_honest():
    report = render_weekly_report(
        {
            "period_pnl": None,
            "period_pnl_pct": None,
            "asset_value": 29620,
            # benchmark, drawdown, turnover, fees all missing
            "decisions": {"entries": 0, "adds": 0, "trims": 0, "exits": 0, "blocked": 0},
        }
    )
    assert "P&L        unavailable (unavailable)" in report
    assert "Benchmark" not in report  # section absent when benchmark unknown
    assert "Max DD" not in report
    assert "$0" not in report
    assert "0 entries" in report

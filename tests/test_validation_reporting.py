from tradehub_research.validation.reporting import (
    flow_adjusted_profit,
    normalize_broker_performance,
    period_return,
    render_daily_report,
    render_weekly_report,
)


def test_normalize_sorts_and_sanitizes():
    rows = [
        {"date": "2026-08-02", "asset_value": 100.0, "daily_pnl": 1.0},
        {"date": "2026-08-01", "asset_value": 99.0, "daily_pnl": -1.0},
        {"date": "2026-08-01", "asset_value": 98.0, "daily_pnl": -2.0, "unknown_key": "x"},
    ]
    normalized = normalize_broker_performance(rows)["rows"]
    assert len(normalized) == 3
    assert [row["date"] for row in normalized] == ["2026-08-01", "2026-08-01", "2026-08-02"]
    assert "unknown_key" not in normalized[0]
    assert normalized[0]["daily_pnl"] == -1.0  # first same-date row wins the sort; no dedupe


def test_normalize_keeps_missing_broker_fields_unknown():
    """A broker field the broker does not report is UNKNOWN (None), never 0.0."""
    normalized = normalize_broker_performance([{"date": "2026-08-01", "asset_value": 100.0}])[
        "rows"
    ]
    assert normalized[0]["daily_pnl"] is None
    assert normalized[0]["cash_balance"] is None
    assert normalized[0]["gross_position_value"] is None
    assert normalized[0]["deposits"] is None
    assert normalized[0]["withdrawals"] is None


def test_flow_adjusted_profit_excludes_cash_flows():
    profit = flow_adjusted_profit(
        end_asset=11_000.0,
        start_asset=10_000.0,
        deposits=500.0,
        withdrawals=200.0,
    )
    assert profit == 700.0  # 1000 - 500 + 200


def test_flow_adjusted_profit_unknown_leg_is_none():
    assert flow_adjusted_profit(11_000.0, 10_000.0, None, 0.0) is None


def test_period_return():
    assert period_return(10_000.0, 11_000.0) == 0.10  # fraction; *100 = 10%
    assert period_return(None, 11_000.0) is None
    assert period_return(0.0, 0.0) is None


def test_daily_report_missing_values_are_unavailable_never_zero():
    report = render_daily_report({})
    assert "$0" not in report
    assert "unavailable" in report
    assert report.count("unavailable") >= 5  # portfolio/today/realized/unrealized/cash
    assert "Actions:" in report
    assert "No action" in report
    assert "Predictions: 0" in report


def test_daily_report_presents_broker_values_when_present():
    report = render_daily_report(
        {
            "asset_value": 100_000.0,
            "daily_pnl": 250.0,
            "daily_pnl_pct": 0.25,
            "realized_pnl": 100.0,
            "unrealized_pnl": 150.0,
            "cash_balance": 40_000.0,
            "actions": "1 PAPER execution(s)",
            "predictions": 10_632,
            "new_matured": 3,
            "system_health": "healthy",
        }
    )
    assert "Portfolio: $100,000.00" in report
    assert "Today: +$250.00 (+0.25%)" in report
    assert "Realized: +$100.00" in report
    assert "Unrealized: +$150.00" in report
    assert "Cash: $40,000.00" in report
    assert "1 PAPER execution(s)" in report
    assert "Predictions: 10632" in report
    assert "New matured outcomes: 3" in report
    assert "Data/system health: healthy" in report


def test_daily_report_explicit_broker_zero_is_zero():
    report = render_daily_report({"daily_pnl": 0.0, "asset_value": 0.0})
    assert "Today: +$0.00 (unavailable)" in report
    assert "Portfolio: $0.00" in report


def test_weekly_report_shape():
    report = render_weekly_report(
        {
            "asset_value": 100_000.0,
            "week_pnl": 500.0,
            "week_pnl_pct": 0.5,
            "since_start_pnl": 2_000.0,
            "since_start_pct": 2.0,
            "benchmark_pct": 1.2,
            "relative_pp": -0.7,
            "trades": 1,
            "blocked": 1,
            "no_action_cycles": 2,
            "predictions": 10_632,
            "matured_21": 4,
            "matured_63": 1,
            "matured_126": 0,
            "matured_252": 0,
            "system": "healthy",
        }
    )
    assert "TRADEHUB · WEEKLY" in report
    assert "Portfolio: $100,000.00" in report
    assert "Week: +$500.00 (+0.50%)" in report
    assert "Since start: +$2,000.00 (+2.00%)" in report
    assert "Benchmark: +1.20%" in report
    assert "Relative: -0.70 pp" in report
    assert "Trades: 1" in report
    assert "Blocked/refused: 1" in report
    assert "No-action cycles: 2" in report
    assert "21-session matured: 4" in report
    assert "63-session matured: 1" in report
    assert "252-session matured: 0" in report
    assert "System:" in report


def test_weekly_report_missing_values_are_unavailable():
    report = render_weekly_report({})
    assert "$0" not in report
    assert report.count("unavailable") >= 4

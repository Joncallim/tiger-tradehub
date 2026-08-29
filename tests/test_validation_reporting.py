from tradehub_research.validation.reporting import (
    flow_adjusted_profit,
    normalize_broker_performance,
    period_return,
    render_daily_report,
    render_weekly_report,
)


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


def test_flow_adjusted_profit_reconciliation():
    # 30k end, 20k start, 5k deposit, 1k withdrawal
    profit = flow_adjusted_profit(30000, 20000, 5000, 1000)
    assert profit == 30000 - 20000 - 5000 + 1000  # 6000
    assert period_return(20000, 30000) == 0.5


def test_daily_report_is_deterministic_text():
    report = render_daily_report(
        {
            "asset_value": 29620,
            "daily_pnl": 123.45,
            "daily_pnl_pct": 0.42,
            "cash": 10000,
            "gross_position_value": 19620,
            "position_count": 6,
            "realized_pnl": 42,
            "unrealized_pnl": 268,
            "fees": 3.2,
            "trades_today": {"buys": 1, "sells": 0, "blocked": 0},
            "status": "No action recommended.",
        }
    )
    assert report.startswith("TRADEHUB · DAILY")
    assert "+$123.45" in report
    assert "(+0.42%)" in report
    assert "Cash 34%" in report  # 10000/29620 = 33.76%
    assert "1 buy · 0 sells · 0 blocked" in report
    # determinism: identical input -> identical output
    assert report == render_daily_report(
        {
            "asset_value": 29620,
            "daily_pnl": 123.45,
            "daily_pnl_pct": 0.42,
            "cash": 10000,
            "gross_position_value": 19620,
            "position_count": 6,
            "realized_pnl": 42,
            "unrealized_pnl": 268,
            "fees": 3.2,
            "trades_today": {"buys": 1, "sells": 0, "blocked": 0},
            "status": "No action recommended.",
        }
    )


def test_weekly_report_computes_active_return_itself():
    report = render_weekly_report(
        {
            "period_pnl": 310.2,
            "period_pnl_pct": 1.06,
            "benchmark_pct": 0.88,
            "since_start_pct": 2.5,
            "max_drawdown_pct": -1.3,
            "fees": 3.2,
            "turnover_pct": 12,
            "decisions": {"entries": 2, "adds": 1, "trims": 0, "exits": 1, "blocked": 1},
            "contributors": {"best": "NVDA", "best_pnl": 88, "worst": "XYZ", "worst_pnl": -31},
            "research_health": "signals evaluated: 12; data gaps: 0",
        }
    )
    assert report.startswith("TRADEHUB · WEEK")
    assert "+$310.20" in report
    assert "Active     +0.18 pp" in report  # 1.06 - 0.88 computed by the renderer
    assert "2 entries / 1 adds / 0 trims / 1 exits / 1 blocked" in report
    assert "NVDA" in report and "XYZ" in report
    assert "RESEARCH HEALTH" in report

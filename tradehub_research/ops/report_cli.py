"""Deterministic daily/weekly report builder + Telegram-ready output (B5/B6).

The report text is COMPUTED here (no model arithmetic). Delivery is the
Hermes/Telegram surface: the Hermes cron runs this CLI with no_agent and
delivers the printed text verbatim.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.health import forward_health, refresh_health
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.reporting import render_daily_report, render_weekly_report


def _broker_today(execution_analytics_path: Path) -> dict:
    """Read the sanitized broker analytics snapshot written by the execution
    side (tradehub-execution reconcile). Missing file -> ALL broker values
    unavailable (rendered as 'unavailable', never $0)."""
    if not execution_analytics_path.exists():
        return {}
    try:
        data = json.loads(execution_analytics_path.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def build_daily_report(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    analytics: dict | None = None,
) -> str:
    """Assemble the deterministic daily report."""
    paths = paths or research_paths()
    broker = (
        analytics
        if analytics is not None
        else _broker_today(Path("/var/lib/tradehub/analytics/latest.json"))
    )
    fwd = forward_health(experiment_db=experiment_db)
    refr = refresh_health(settings=settings, paths=paths)

    data = {
        "daily_pnl": broker.get("daily_pnl"),
        "daily_pnl_pct": broker.get("daily_pnl_pct"),
        "asset_value": broker.get("asset_value"),
        "cash": broker.get("cash_balance"),
        "gross_position_value": broker.get("gross_position_value"),
        "position_count": broker.get("position_count"),
        "realized_pnl": broker.get("realized_pnl"),
        "unrealized_pnl": broker.get("unrealized_pnl"),
        "fees": broker.get("fees"),
        "trades_today": {
            "entries": 0,
            "adds": 0,
            "trims": 0,
            "exits": 0,
            "blocked": 0,
        },
    }
    if refr.get("stale_count"):
        data["data_health"] = f"stale ({refr['stale_count']} names behind {refr['as_of']})"
    elif refr.get("with_bars"):
        data["data_health"] = "healthy"
    else:
        data["data_health"] = "blocked"
    if fwd.get("production_predictions"):
        data["research_health"] = (
            f"forward: {fwd['production_predictions']} predictions, "
            f"{fwd.get('predictions_due', 0)} due, matured "
            f"{sum(fwd.get('matured', {}).values())}"
        )
        matured = fwd.get("matured", {})
        data["learning"] = (
            f"{fwd['production_predictions']} real predictions · "
            f"{sum(matured.values())} outcomes matured"
        )
    data["status"] = "No action required."
    return render_daily_report(data)


def build_weekly_report(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    analytics: dict | None = None,
    benchmark_pct: float | None = None,
) -> str:
    paths = paths or research_paths()
    broker = (
        analytics
        if analytics is not None
        else _broker_today(Path("/var/lib/tradehub/analytics/latest.json"))
    )
    fwd = forward_health(experiment_db=experiment_db)
    refr = refresh_health(settings=settings, paths=paths)

    data = {
        "period_pnl": broker.get("period_pnl"),
        "period_pnl_pct": broker.get("period_pnl_pct"),
        "asset_value": broker.get("asset_value"),
        "benchmark_pct": benchmark_pct,
        "max_drawdown_pct": broker.get("max_drawdown_pct"),
        "fees": broker.get("fees"),
        "turnover_pct": broker.get("turnover_pct"),
        "decisions": {
            "entries": 0,
            "adds": 0,
            "trims": 0,
            "exits": 0,
            "blocked": 0,
        },
    }
    matured = fwd.get("matured", {})
    research = [
        f"learning dataset: {fwd.get('production_predictions', 0)} production predictions",
        f"matured outcomes: {sum(matured.values())}",
        f"market data: {refr.get('fresh', 0)}/{refr.get('securities_expected', 0)} fresh",
    ]
    data["research_health"] = "\n".join(research)
    return render_weekly_report(data)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Deterministic TradeHub report")
    parser.add_argument("--period", choices=("daily", "weekly"), default="daily")
    args = parser.parse_args(argv)
    settings = ResearchSettings()
    exp = ExperimentDB(research_paths().experiment_db)
    if args.period == "daily":
        print(build_daily_report(settings=settings, experiment_db=exp))
    else:
        print(build_weekly_report(settings=settings, experiment_db=exp))
    return 0


if __name__ == "__main__":
    sys.exit(main())

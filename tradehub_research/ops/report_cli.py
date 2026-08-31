"""Deterministic daily/weekly report builder + Telegram-ready output (observation mode).

The report text is COMPUTED here (no model arithmetic; broker analytics are
the accounting source of truth). Delivery is the Hermes/Telegram surface:
the Hermes cron runs this CLI with no_agent and delivers the printed text
verbatim.

Daily shape (2026-08-31 owner brief):
  Portfolio / Today / Realized / Unrealized / Cash
  Actions (or No action)
  Learning: Predictions / New matured outcomes / Data-system health

Weekly shape:
  Portfolio / Week / Since start / Benchmark / Relative
  Trades / Blocked-refused / No-action cycles
  Learning: Predictions + 21/63/126/252-session matured
  System
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from tradehub_research.config import ResearchSettings
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.health import forward_health, refresh_health
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.reporting import render_daily_report, render_weekly_report

ANALYTICS_DIR = Path("/var/lib/tradehub/analytics")
LATEST = ANALYTICS_DIR / "latest.json"
HISTORY = ANALYTICS_DIR / "history.jsonl"
LEDGER = Path("/var/lib/tradehub-research/autonomy/paper_run_ledger.jsonl")


def _broker_today(path: Path) -> dict:
    """Sanitized broker snapshot; missing file -> ALL values unavailable."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("date"):
            rows.append(parsed)
    rows.sort(key=lambda item: item["date"])
    return rows


def _week_ago_value(rows: list[dict]) -> float | None:
    """asset_value closest to but not after (latest_date - 7 days)."""
    if not rows:
        return None
    latest_date = rows[-1]["date"]
    try:
        target = (date.fromisoformat(latest_date) - timedelta(days=7)).isoformat()
    except ValueError:
        return None
    prior = [r for r in rows if r["date"] <= target]
    if not prior:
        return None
    return prior[-1].get("asset_value")


def _flow_adjusted(rows: list[dict]) -> float | None:
    """Since-start P&L = end - start - deposits + withdrawals (None on unknowns)."""
    if len(rows) < 2:
        return None
    end = rows[-1].get("asset_value")
    start = rows[0].get("asset_value")
    if end is None or start is None:
        return None
    deposits = sum(r.get("deposits") or 0 for r in rows)
    withdrawals = sum(r.get("withdrawals") or 0 for r in rows)
    if any(r.get("deposits") is None for r in rows) and deposits == 0:
        deposits = None  # UNKNOWN deposits cannot be treated as zero
    if any(r.get("withdrawals") is None for r in rows) and withdrawals == 0:
        withdrawals = None
    if deposits is None or withdrawals is None:
        return None
    return end - start - deposits + withdrawals


def _ledger_actions(ledger_path: Path, today: str) -> tuple[int, int]:
    """(executions, refusals) recorded today by the autonomous runner."""
    executions = refusals = 0
    if not ledger_path.exists():
        return 0, 0
    for line in ledger_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if str(entry.get("at", ""))[:10] != today:
            continue
        if entry.get("decision") == "EXECUTED":
            executions += 1
        else:
            refusals += 1
    return executions, refusals


def _system_health(fwd: dict, refr: dict, refr_count: int) -> str:
    flags = []
    if refr.get("stale_count"):
        flags.append(f"{refr['stale_count']} stale data names")
    elif refr_count:
        flags.append("data healthy")
    if not fwd.get("production_predictions"):
        flags.append("no production predictions")
    due = fwd.get("predictions_due", 0)
    if due:
        flags.append(f"{due} outcomes due")
    return "healthy" if not flags else "; ".join(flags)


def build_daily_report(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    analytics: dict | None = None,
) -> str:
    paths = paths or research_paths()
    broker = analytics if analytics is not None else _broker_today(LATEST)
    fwd = forward_health(experiment_db=experiment_db)
    refr = refresh_health(settings=settings, paths=paths)
    executions, refusals = _ledger_actions(LEDGER, date.today().isoformat())

    actions = []
    if executions:
        actions.append(f"{executions} PAPER execution(s)")
    if refusals:
        actions.append(f"{refusals} refused/blocked")
    actions_text = "; ".join(actions) if actions else "No action"

    matured = fwd.get("matured_by_horizon", {})
    data = {
        "asset_value": broker.get("asset_value"),
        "daily_pnl": broker.get("daily_pnl"),
        "daily_pnl_pct": broker.get("daily_pnl_pct"),
        "realized_pnl": broker.get("realized_pnl"),
        "unrealized_pnl": broker.get("unrealized_pnl"),
        "cash_balance": broker.get("cash_balance"),
        "actions": actions_text,
        "predictions": fwd.get("production_predictions", 0),
        "new_matured": sum(matured.values()),
        "system_health": _system_health(fwd, refr, 0),
    }
    return render_daily_report(data)


def build_weekly_report(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    analytics: dict | None = None,
    benchmark_pct: float | None = None,
    history: list[dict] | None = None,
) -> str:
    paths = paths or research_paths()
    broker = analytics if analytics is not None else _broker_today(LATEST)
    rows = history if history is not None else _history(HISTORY)
    fwd = forward_health(experiment_db=experiment_db)
    refr = refresh_health(settings=settings, paths=paths)

    asset_value = broker.get("asset_value")
    week_ago = _week_ago_value(rows)
    week_pnl = None if (asset_value is None or week_ago is None) else asset_value - week_ago
    week_pct = None if (week_pnl is None or not week_ago) else week_pnl / week_ago * 100
    since_start = _flow_adjusted(rows)
    first_value = rows[0].get("asset_value") if rows else None
    since_start_pct = (
        None if (since_start is None or not first_value) else since_start / first_value * 100
    )

    matured = fwd.get("matured_by_horizon", {})
    executions, refusals = _ledger_actions(LEDGER, date.today().isoformat())
    system = []
    if refr.get("stale_count"):
        system.append(f"{refr['stale_count']} stale data names")
    elif refr.get("with_bars"):
        system.append("data healthy")
    if executions:
        system.append(f"{executions} PAPER execution(s) today")

    data = {
        "asset_value": asset_value,
        "week_pnl": week_pnl,
        "week_pnl_pct": week_pct,
        "since_start_pnl": since_start,
        "since_start_pct": since_start_pct,
        "benchmark_pct": benchmark_pct,
        "relative_pp": None
        if (week_pct is None or benchmark_pct is None)
        else week_pct - benchmark_pct,
        "trades": executions,
        "blocked": refusals,
        "no_action_cycles": 0,
        "predictions": fwd.get("production_predictions", 0),
        "matured_21": matured.get("21", 0),
        "matured_63": matured.get("63", 0),
        "matured_126": matured.get("126", 0),
        "matured_252": matured.get("252", 0),
        "system": "; ".join(system) if system else "healthy",
    }
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

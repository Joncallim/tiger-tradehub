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
from typing import NamedTuple

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


class HistoryRows(NamedTuple):
    """Broker history rows, plus the error type when the file could not be read."""

    rows: list[dict]
    error: str | None = None


def _history(path: Path) -> HistoryRows:
    """Broker history rows; an ABSENT history is a documented empty history.

    Existence is probed with an explicit ``stat()``, never ``Path.exists()``:
    ``exists()`` swallows every ``OSError`` (EACCES included) and returns
    ``False``, which would conflate "the history cannot be read" with "there is
    no history yet". An UNREADABLE history is reported with its error type (the
    P&L then renders as ``unavailable``, never ``$0``) and never raises.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return HistoryRows([])
    except OSError as exc:
        return HistoryRows([], type(exc).__name__)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return HistoryRows([], type(exc).__name__)
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("date"):
            rows.append(parsed)
    rows.sort(key=lambda item: item["date"])
    return HistoryRows(rows)


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


class LedgerActions(NamedTuple):
    """Today's autonomous-runner actions, or all-UNKNOWN when unreadable."""

    executions: int | None
    refusals: int | None
    unknown: int | None
    error: str | None = None


def _ledger_actions(ledger_path: Path, today: str) -> LedgerActions:
    """Today's (executions, refusals, unknown) autonomous-runner actions.

    ONLY per-proposal outcomes are ACTIONS. A run receipt
    (``kind=runner_run_receipt_v1``: IDLE_EMPTY_INBOX / OK / BLOCKED summaries)
    is the durable record of one INVOCATION, not an action; counting receipts
    as refusals made an enabled-but-idle runner render as "N refused/blocked"
    in the daily report. Per-proposal refusals are durable inside the receipt
    as ``refusal_count``, which is where they are counted from.

    An INDETERMINATE submit (broker outcome UNKNOWN) is reported as UNKNOWN --
    neither an execution nor a refusal, because claiming either would be a
    false statement about what happened.

    An ABSENT ledger is a documented zero-action day. An UNREADABLE ledger
    (permission change, or a write truncated mid-byte by a killed oneshot
    service) yields ``None`` counts plus the error type: the report says the
    ledger is unavailable rather than claiming nothing happened, and it must
    never raise out of report generation.

    Existence is probed with an explicit ``stat()``, never ``Path.exists()``:
    ``exists()`` swallows every ``OSError`` (EACCES included) and returns
    ``False``, which is how a genuine read failure used to render as a
    legitimate "No action" day. Only ``FileNotFoundError`` is an absence.
    """
    try:
        ledger_path.stat()
    except FileNotFoundError:
        return LedgerActions(0, 0, 0)
    except OSError as exc:
        return LedgerActions(None, None, None, type(exc).__name__)
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return LedgerActions(None, None, None, type(exc).__name__)
    executions = refusals = unknown = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or str(entry.get("at", ""))[:10] != today:
            continue
        if entry.get("kind") == "runner_run_receipt_v1":
            try:
                refusals += int(entry.get("refusal_count") or 0)
            except (TypeError, ValueError):
                unknown += 1
            continue
        if entry.get("decision") == "EXECUTED":
            executions += 1
        else:
            # Unclassifiable / indeterminate: never silently swallowed.
            unknown += 1
    return LedgerActions(executions, refusals, unknown)


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
    acts = _ledger_actions(LEDGER, date.today().isoformat())

    actions = []
    if acts.error:
        # An UNREADABLE ledger is reported as unavailable, never as "No action".
        actions_text = f"action ledger unavailable ({acts.error})"
    else:
        if acts.executions:
            actions.append(f"{acts.executions} PAPER execution(s)")
        if acts.refusals:
            actions.append(f"{acts.refusals} refused/blocked")
        if acts.unknown:
            actions.append(f"{acts.unknown} outcome(s) unknown")
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
    if history is not None:
        rows, history_error = history, None
    else:
        loaded = _history(HISTORY)
        rows, history_error = loaded.rows, loaded.error
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
    acts = _ledger_actions(LEDGER, date.today().isoformat())
    system = []
    if refr.get("stale_count"):
        system.append(f"{refr['stale_count']} stale data names")
    elif refr.get("with_bars"):
        system.append("data healthy")
    if acts.executions:
        system.append(f"{acts.executions} PAPER execution(s) today")
    if acts.unknown:
        system.append(f"{acts.unknown} outcome(s) unknown")
    if acts.error:
        system.append(f"action ledger unavailable ({acts.error})")
    if history_error:
        system.append(f"broker history unavailable ({history_error})")

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
        # UNKNOWN (not 0) when the ledger could not be read.
        "trades": acts.executions if acts.executions is not None else "unavailable",
        "blocked": acts.refusals if acts.refusals is not None else "unavailable",
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

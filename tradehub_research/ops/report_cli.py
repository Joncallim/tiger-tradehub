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
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

from tradehub_research.config import ResearchSettings
from tradehub_research.ops.common import ResearchPaths, research_paths
from tradehub_research.ops.health import forward_health, freshness_accounting, refresh_health
from tradehub_research.validation.benchmark import load_latest_benchmark, window_return_pct
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


def _freshness_flags(accounting: dict | None, accounting_error: str | None) -> list[str]:
    """Fleet accounting flags, from the freshness audit's own authority.

    The audit already EXCLUDES legitimately unfetchable names (delisted / invalid
    symbol / no-trade) from eligibility. Describing those exclusions as staleness
    is how the live report came to headline "42 stale data names" against a real
    backlog of 2 (LCGMF, TRLEF) -- an incident count that the health watch, which
    reads the same audit, never corroborated. The genuinely unresolved names are
    named here so a real gap cannot hide inside a total.
    """
    if accounting_error:
        return [f"data-freshness audit unavailable ({accounting_error})"]
    if not accounting:
        return []
    flags: list[str] = []
    unresolved = accounting.get("unresolved_count", 0)
    if unresolved:
        names = ", ".join(str(ticker) for ticker in accounting.get("unresolved", [])[:5])
        flags.append(f"{unresolved} unresolved data gap(s): {names}")
    else:
        flags.append("no unresolved data gaps")
    if accounting.get("excluded_exceptions"):
        flags.append(
            f"{accounting['excluded_exceptions']} names excluded as legitimate exceptions "
            "(delisted/unfetchable)"
        )
    if accounting.get("lagging_within_window"):
        flags.append(f"{accounting['lagging_within_window']} lagging within the rolling window")
    return flags


def _forward_flags(fwd: dict) -> list[str]:
    """Forward-ledger flags. ``predictions_due`` means the session horizon elapsed."""
    flags: list[str] = []
    if not fwd.get("production_predictions"):
        flags.append("no production predictions")
    mature = fwd.get("predictions_due") or 0
    if mature:
        flags.append(f"{mature} outcomes mature")
    awaiting = (fwd.get("predictions_due_check") or 0) - mature
    if awaiting > 0:
        flags.append(f"{awaiting} scheduled for maturity check (horizon not elapsed)")
    return flags


def _data_health_line(fwd: dict, accounting: dict | None, accounting_error: str | None) -> str:
    """The data/system health line, derived from the audit's authority."""
    flags = _freshness_flags(accounting, accounting_error) + _forward_flags(fwd)
    return "healthy" if not flags else "; ".join(flags)


def _system_health(
    fwd: dict,
    refr: dict,
    refr_count: int,
    *,
    accounting: dict | None = None,
    accounting_error: str | None = None,
) -> str:
    """Compose the daily health line.

    When the freshness audit's accounting is available it is authoritative and
    the ``refr``-based wording is not used at all; the legacy wording survives
    only as the fallback for a run where the audit could not be read.
    """
    if accounting is not None or accounting_error is not None:
        flags = _freshness_flags(accounting, accounting_error)
    else:
        flags = []
        if refr.get("stale_count"):
            flags.append(f"{refr['stale_count']} stale data names")
        elif refr_count:
            flags.append("data healthy")
    flags = flags + _forward_flags(fwd)
    return "healthy" if not flags else "; ".join(flags)


def _freshness_view(*, settings, paths, experiment_db) -> tuple[dict | None, str | None]:
    """The audit's fleet accounting, or ``(None, error type)``.

    A report surface must always deliver: if the audit cannot run (unreadable
    database, path/permission change), the report says the audit is unavailable
    AND names the error type rather than falling back to a second, disagreeing
    staleness derivation or failing to send at all.
    """
    try:
        return (
            freshness_accounting(settings=settings, paths=paths, experiment_db=experiment_db),
            None,
        )
    except Exception as exc:  # noqa: BLE001 -- a report must never fail to deliver
        return None, type(exc).__name__


def _week_benchmark(
    *, experiment_db, settings, paths, rows: list[dict]
) -> tuple[float | None, str | None]:
    """The weekly benchmark return over the report's own window.

    Returns ``(pct, note)``. ``benchmark_pct`` was a parameter ``main()`` never
    supplied, so "Benchmark:" and "Relative:" could never render a number at all.
    Wiring it exposes two gaps the report must STATE rather than render as a bare
    "unavailable": a vintage whose series does not reach the window, and a pinned
    artifact whose recorded cache path points into the pre-migration checkout.
    Never extrapolates: an uncovered window stays unavailable, with the reason.
    """
    if not rows or experiment_db is None:
        return None, None
    # ``paths.raw_cache`` is the DEPLOYMENT-AWARE source: it derives from
    # TRADEHUB_RESEARCH_DIR / TRADEHUB_RAW_CACHE, both of which the report cron
    # passes through its sudo --preserve-env list. ``settings.adapter_cache_dir``
    # comes from RESEARCH_ADAPTER_CACHE_DIR, which that list does NOT carry, so
    # there it silently falls back to the in-repo default and the pinned cache
    # file is reported missing -- which is exactly what the deployed weekly
    # report did ("benchmark unavailable (ValueError)") while an operator shell
    # with the full env rendered the vintage-coverage reason.
    raw_cache = getattr(paths, "raw_cache", None) or getattr(settings, "adapter_cache_dir", None)
    if raw_cache is None:
        return None, "benchmark unavailable (benchmark cache directory unknown)"
    end = str(rows[-1].get("date") or "")
    try:
        start = (date.fromisoformat(end) - timedelta(days=7)).isoformat()
    except ValueError:
        return None, None
    try:
        vintage = load_latest_benchmark(experiment_db, Path(raw_cache))
    except (ValueError, OSError, sqlite3.Error, TypeError, AttributeError) as exc:
        return None, f"benchmark unavailable ({type(exc).__name__})"
    pct = window_return_pct(vintage.series, start, end)
    if pct is None:
        return None, (
            f"benchmark vintage {vintage.vintage_label} ends {vintage.last_session}; "
            f"window {start}..{end} not covered"
        )
    return pct, None


def build_daily_report(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    analytics: dict | None = None,
) -> str:
    paths = paths or research_paths()
    broker = analytics if analytics is not None else _broker_today(LATEST)
    # The report owns the reporting day (it is also the action-ledger day), and
    # passes it down so "new today" is measured on exactly the same clock.
    fwd = forward_health(experiment_db=experiment_db, paths=paths, reporting_day=date.today())
    refr = refresh_health(settings=settings, paths=paths)
    acts = _ledger_actions(LEDGER, date.today().isoformat())
    accounting, accounting_error = _freshness_view(
        settings=settings, paths=paths, experiment_db=experiment_db
    )

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

    data = {
        "asset_value": broker.get("asset_value"),
        "daily_pnl": broker.get("daily_pnl"),
        "daily_pnl_pct": broker.get("daily_pnl_pct"),
        "realized_pnl": broker.get("realized_pnl"),
        "unrealized_pnl": broker.get("unrealized_pnl"),
        "cash_balance": broker.get("cash_balance"),
        "actions": actions_text,
        "predictions": fwd.get("production_predictions", 0),
        # NEW TODAY, not lifetime: production outcomes appended on the report's
        # own day, from the durable appended_at timestamp. The cumulative
        # per-horizon totals stay on the weekly report, where they are documented.
        "new_matured": fwd.get("matured_today", 0),
        # Due-but-not-evaluable (expected entry-session bar unavailable): pending
        # by design, never terminalised by elapsed time.
        "awaiting_entry": fwd.get("awaiting_entry"),
        # Horizon elapsed but the required exit evidence is missing: pending and
        # retryable, never a permanent label for a data gap.
        "awaiting_exit": fwd.get("awaiting_exit"),
        "system_health": _system_health(
            fwd, refr, 0, accounting=accounting, accounting_error=accounting_error
        ),
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
    accounting, accounting_error = _freshness_view(
        settings=settings, paths=paths, experiment_db=experiment_db
    )
    # The benchmark is measured over the SAME window as the portfolio's week. It
    # is wired here because it was previously a parameter no caller ever set, so
    # "Benchmark:" and "Relative:" could never render a number at all.
    if benchmark_pct is None:
        benchmark_pct, benchmark_note = _week_benchmark(
            experiment_db=experiment_db, settings=settings, paths=paths, rows=rows
        )
    else:
        benchmark_note = None

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
    if accounting is not None or accounting_error is not None:
        system.extend(_freshness_flags(accounting, accounting_error))
    else:
        # Legacy wording, used ONLY when the audit could not be read.
        if refr.get("stale_count"):
            system.append(f"{refr['stale_count']} stale data names")
        elif refr.get("with_bars"):
            system.append("data healthy")
    if benchmark_note:
        system.append(benchmark_note)
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

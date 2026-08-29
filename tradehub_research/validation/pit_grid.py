"""Packet C: monthly PIT evaluation grid (handoff sec 6.1).

For each evaluation month the grid chooses a deterministic timestamp AFTER
the relevant monthly EOD data is knowable, using the repo's existing
20:15 America/New_York session-end convention (hunters/common.py
BAR_ELIGIBLE_HHMM) on the last eligible US session of each month. The
event-time secondary grid keys event/informed-activity evaluation to the
first knowable public event timestamp (handoff sec 6.2) -- event rows are
NEVER mixed into the monthly sample as if independent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tradehub_research.db import normalize_ts
from tradehub_research.hunters.common import BAR_ELIGIBLE_HHMM, NEW_YORK


def _session_end_utc(day: date) -> str:
    local = datetime(
        day.year, day.month, day.day, BAR_ELIGIBLE_HHMM[0], BAR_ELIGIBLE_HHMM[1], tzinfo=NEW_YORK
    )
    return normalize_ts(local.astimezone(ZoneInfo("UTC")).isoformat())


def monthly_pit_grid(coverage_start: str, coverage_end: str) -> list[str]:
    """One evaluation timestamp per month: 20:15 NY on the last day of each
    month within [coverage_start, coverage_end].

    Returns RFC3339 UTC timestamps, ascending. The timestamp is strictly
    AFTER that month's EOD data is knowable (the session-end convention),
    so evidence with PAT on or before it is the correct PIT input set.
    """
    start = date.fromisoformat(coverage_start[:10])
    end = date.fromisoformat(coverage_end[:10])
    timestamps: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last_day = _last_calendar_day(year, month)
        timestamps.append(_session_end_utc(last_day))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return timestamps


def _last_calendar_day(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def event_time_grid(public_available_times: list[str]) -> list[str]:
    """Secondary event-time evaluation grid (handoff sec 6.2): the sorted
    unique public-available timestamps of the event/informed-activity
    evidence itself. Each event row is evaluated at its FIRST KNOWABLE
    public timestamp; event rows are labeled with their evaluation mode and
    never pooled into the monthly grid as independent observations."""
    return sorted({normalize_ts(ts) for ts in public_available_times})


def grid_mode_label(grid_timestamp: str, monthly_timestamps: list[str]) -> str:
    """Label whether a grid timestamp belongs to the monthly grid or an
    event-time secondary grid -- evaluation mode must never be ambiguous."""
    if grid_timestamp in set(monthly_timestamps):
        return "monthly"
    return "event_time"

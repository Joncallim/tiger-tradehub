"""US equity market calendar (NYSE session days + holidays).

Why this exists
---------------
`ops/common.last_completed_us_session` is weekday-only and says so:

    # US equity sessions: Monday-Friday. No exchange-holiday calendar is
    # embedded -- a missing session simply has no bar (freshness checks report
    # it honestly as absent, never backfilled).

That is wrong for *freshness* purposes: on the day after Thanksgiving, or on
Juneteenth, or on Good Friday, the "expected latest session" is the previous
real session -- not the holiday. Without a calendar the freshness check would
demand data that cannot exist, and any remediation would spend provider quota
chasing a session that never happened.

This module is the single authority for "which session should exist". It is
pure (no I/O, no clock reads except through the injectable ``now``) so every
branch is deterministically testable.

Scope: regular full-day sessions. The handful of 13:00 ET early closes
(day after Thanksgiving, Christmas Eve) still produce a session bar, so they do
not change expected-session arithmetic; they only affect the intraday
availability window, which `expected_latest_session` already absorbs through
its post-close buffer.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
# Regular-session close 16:00 ET; Tiingo EOD for a session is generally
# published well inside this buffer. 20:15 ET matches the repo's existing
# BAR_ELIGIBLE_HHMM publication boundary (hunters/common.session_close_utc).
POST_CLOSE_BUFFER = timedelta(hours=4, minutes=15)  # 20:15 ET vs 16:00 close
CLOSE_HHMM = (16, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) ``weekday`` of a month. weekday: Mon=0..Sun=6."""
    day = date(year, month, 1)
    offset = (weekday - day.weekday()) % 7
    return day + timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last ``weekday`` of a month."""
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian (Meeus/Jones/Butcher) Easter computation.

    The canonical presentation uses the variable names a..m; `l` is renamed `ell`
    because a single lowercase l is ambiguous (ruff E741).
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(day: date) -> date | None:
    """Sat -> preceding Friday; Sun -> following Monday. Saturday New Year's is
    deliberately NOT observed (NYSE keeps Dec 31 open)."""
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def holidays(year: int) -> dict[date, str]:
    """NYSE full-day closures for ``year`` -> holiday name."""
    out: dict[date, str] = {}

    def add(day: date | None, name: str) -> None:
        if day is not None:
            out[day] = name

    # New Year's Day: Saturday is not observed on the preceding Friday.
    jan1 = date(year, 1, 1)
    if jan1.weekday() != 5:
        add(_observed(jan1), "New Year's Day")

    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, 0, 3), "Washington's Birthday")
    add(_easter_sunday(year) - timedelta(days=2), "Good Friday")
    add(_last_weekday(year, 5, 0), "Memorial Day")

    # Juneteenth (NYSE-observed from 2022).
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")

    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add(_observed(date(year, 12, 25)), "Christmas Day")
    return out


def holiday_name(day: date) -> str | None:
    """Name of the full-day closure on ``day``, else None."""
    return holidays(day.year).get(day)


def is_session_day(day: date) -> bool:
    """True when a regular US equity session is expected on ``day``."""
    return day.weekday() < 5 and holiday_name(day) is None


def previous_session(day: date, *, inclusive: bool = False) -> date:
    """Most recent session on/before ``day`` (``inclusive=False``: strictly before)."""
    cursor = day if inclusive else day - timedelta(days=1)
    for _ in range(400):  # bounded: never loop forever on absurd input
        if is_session_day(cursor):
            return cursor
        cursor -= timedelta(days=1)
    raise ValueError(f"no session found within 400 days before {day}")


def next_session(day: date, *, inclusive: bool = False) -> date:
    """Next session on/after ``day``."""
    cursor = day if inclusive else day + timedelta(days=1)
    for _ in range(400):
        if is_session_day(cursor):
            return cursor
        cursor += timedelta(days=1)
    raise ValueError(f"no session found within 400 days after {day}")


def sessions_in_range(start: date, end: date, *, inclusive: bool = True) -> list[date]:
    """All session days in [start, end] (or (start, end] when not inclusive)."""
    if end < start:
        return []
    cursor = start if inclusive else start + timedelta(days=1)
    out: list[date] = []
    while cursor <= end:
        if is_session_day(cursor):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def count_sessions(start: date, end: date, *, inclusive: bool = True) -> int:
    """Number of expected sessions in the range (holiday- and weekend-aware)."""
    return len(sessions_in_range(start, end, inclusive=inclusive))


def sessions_behind(last_seen: date | None, expected: date) -> int:
    """Expected sessions strictly after ``last_seen`` up to and including ``expected``.

    Zero means "current". A symbol whose last bar IS the expected session is
    current. Weekends and holidays never count against a symbol.
    """
    if last_seen is None:
        return -1  # unknown: no bars at all; callers classify separately
    if last_seen >= expected:
        return 0
    return count_sessions(last_seen, expected, inclusive=False)


def expected_latest_session(now: datetime | None = None) -> date:
    """The session whose EOD data should exist by ``now``.

    Evaluated in EXCHANGE-LOCAL time, which matters twice:
      * the US market date is still the previous ET day during the UTC evening
        (2026-11-27T02:00Z is Thanksgiving Thursday 21:00 ET -- closed, so the
        expected session is Wednesday, not Friday);
      * the close+buffer boundary must survive DST (a fixed UTC offset would
        shift it by an hour twice a year).

    Rules:
      * Before the close + publication buffer on a session day, today's bar is
        not yet due -- the previous session is expected.
      * After the buffer on a session day, today is expected.
      * Weekends, holidays and the ET/UTC day skew never produce a session that
        does not exist.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_et = now.astimezone(NEW_YORK)
    today_et = now_et.date()
    boundary_et = datetime.combine(today_et, time(*CLOSE_HHMM), tzinfo=NEW_YORK) + POST_CLOSE_BUFFER
    if is_session_day(today_et) and now_et >= boundary_et:
        return today_et
    return previous_session(today_et)


__all__ = [
    "count_sessions",
    "expected_latest_session",
    "holiday_name",
    "holidays",
    "is_session_day",
    "next_session",
    "previous_session",
    "sessions_behind",
    "sessions_in_range",
]

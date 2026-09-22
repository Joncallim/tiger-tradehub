"""The ONE authoritative definition of a forward horizon.

WHY THIS MODULE EXISTS (defect found 2026-09-22, before the first production
cohort matured)

Two clocks described the same thing and disagreed:

  * ``forward_collector._outcome_due_date`` wrote a due date from a session
    horizon using ``round(sessions / (252 / 365.25))`` calendar days
    (21 -> 30, 63 -> 91, 126 -> 183, 252 -> 365);
  * ``ops.outcome_maturation`` evaluated realized returns with its own hard-coded
    ``{21: 40, 63: 105, 126: 210, 252: 420}`` day map, and then took the LATEST
    bar at or before the calendar target as the exit.

Mismatched clocks were only half of it. Because the exit was "the latest bar
before the target", a prediction could be appended ``OBSERVED`` while its session
horizon had not completed at all -- and a late run could observe a horizon far
longer than the contract.

THE SEMANTICS (one definition, used by writer and evaluator)

A forward horizon is a number of COMPLETED TRADING SESSIONS after the entry
session. The contract values are 21/63/126/252. Weekends and market holidays are
never sessions, so a horizon is measured on the market calendar -- never in
calendar days, and never by "whatever bars happen to exist".

  * :func:`select_exit_bar` -- the exit bar for the horizon, or ``None`` while the
    horizon is IMMATURE. It never substitutes the latest available bar.
  * :func:`required_exit_session` -- the calendar date that exit session falls on.
  * :func:`session_horizon_due_date` -- the date a NEW prediction's horizon
    completes (what the collector writes into ``outcome_due_date``).
  * :func:`legacy_advisory_due_date` -- the pre-fix approximation, retained only
    to interpret the due dates already frozen into the immutable ledger.

BACKWARD COMPATIBILITY

Existing ``forward_prediction`` rows are immutable and are never rewritten. Their
``outcome_due_date`` values (written with the legacy formula) stay what they are
and remain an **advisory scheduling hint**: they decide when the maturation job
LOOKS at a prediction. Whether a prediction may become ``OBSERVED`` is decided
independently, by counting the realized sessions against :func:`select_exit_bar`
-- so the two clocks can no longer disagree about a horizon.

LAYERING NOTE

This module imports ``ops.market_calendar`` for session arithmetic. That module is
a pure, stdlib-only date utility (holiday table + date walking) with no
operational behaviour, and reimplementing its calendar here is precisely the
second-clock mistake this module exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from tradehub_research.ops.market_calendar import next_session

#: The contract horizons, in completed trading sessions after entry.
HORIZON_SESSIONS: tuple[int, ...] = (21, 63, 126, 252)

#: A US equity trading year, for the legacy calendar approximation only.
SESSIONS_PER_TRADING_YEAR = 252


def _validate(horizon_sessions: int) -> int:
    if horizon_sessions not in HORIZON_SESSIONS:
        raise ValueError(f"horizon_sessions must be one of {HORIZON_SESSIONS}")
    return int(horizon_sessions)


def entry_session_for(as_of: str) -> str:
    """The entry session for a prediction made at ``as_of``.

    Entry is the first session STRICTLY AFTER the information date: a prediction
    cannot be entered on the session whose data produced it.
    """
    return next_session(date.fromisoformat(as_of[:10])).isoformat()


def required_exit_session(entry_session: str, horizon_sessions: int) -> str:
    """Calendar date of the ``horizon_sessions``-th session after the entry session.

    Sessions are counted on the market calendar, so weekends and holidays are
    skipped and never count against the horizon.
    """
    horizon = _validate(horizon_sessions)
    session = date.fromisoformat(entry_session[:10])
    for _ in range(horizon):
        session = next_session(session)
    return session.isoformat()


def session_horizon_due_date(as_of: str, horizon_sessions: int) -> str:
    """The date a new prediction's horizon completes -- what gets written.

    Deterministic from the market calendar alone: no bars required, so a
    prediction's due date never depends on when its data happens to arrive. A
    symbol whose first bar lands late is handled by the evaluator, which counts
    realized sessions and keeps the prediction pending until they exist.
    """
    return required_exit_session(entry_session_for(as_of), horizon_sessions)


def legacy_advisory_due_date(as_of: str, horizon_sessions: int) -> str:
    """The pre-fix calendar approximation, kept to interpret existing rows.

    Preserved byte-for-byte (``as_of + round(sessions / (252/365.25))`` days) so
    the due dates already frozen in ``forward_prediction`` remain interpretable.
    New predictions use :func:`session_horizon_due_date` instead. The two agree
    within a few sessions, which is why existing rows keep working as advisory
    scheduling hints.
    """
    horizon = _validate(horizon_sessions)
    day = date.fromisoformat(as_of[:10])
    days = round(horizon / (SESSIONS_PER_TRADING_YEAR / 365.25))
    return (day + timedelta(days=days)).isoformat()


def select_exit_bar(bars: Sequence[Any], horizon_sessions: int) -> Any | None:
    """The exit bar for ``horizon_sessions`` completed sessions after entry.

    ``bars`` must be the canonical session bars STRICTLY AFTER the entry session,
    ordered oldest-first, one per session. Returns ``None`` when fewer than
    ``horizon_sessions`` sessions exist -- the horizon is immature, and the caller
    must not substitute the latest available bar for it.
    """
    horizon = _validate(horizon_sessions)
    if len(bars) < horizon:
        return None
    return bars[horizon - 1]


def is_mature(bars: Iterable[Any], horizon_sessions: int) -> bool:
    """Whether the realized bars already contain the full horizon."""
    horizon = _validate(horizon_sessions)
    return len(list(bars)) >= horizon

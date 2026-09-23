"""Canonical outcome pricing -- ONE definition for both outcome planes.

Two planes build realized outcomes from the same evidence:

* ``validation/outcome_builder.py`` -- FROZEN dataset snapshots (research/replay);
* ``ops/outcome_maturation.py``      -- the LIVE forward ledger.

They may differ in missing-data policy (a frozen snapshot may terminally censor at
its final dataset boundary; the live ledger keeps recoverable entry/exit gaps
pending), but an OBSERVED result must mean the same thing in both. This module owns
that shared meaning so it is not maintained twice:

  * entry-session selection: the calendar's expected entry session, taken from the
    canonical EVIDENCE layer (``portfolio.prices._visible_records`` ->
    ``_bar_records``), so supersession, withdrawals, publication visibility and
    approved provenance are respected exactly as everywhere else in the repo;
  * entry price convention: next-session OPEN, else next-session CLOSE recorded
    explicitly as a fallback;
  * exit: the exact required horizon session's canonical CLOSE;
  * ``raw_return``: close-to-entry price ratio, unadjusted;
  * ``total_return``: corporate-action aware (splits/dividends), per the
    documented Phase-5 convention
    ``(close_t * cum_factor + cum_dividend) / entry_price - 1``.

Evidence is only ever consumed through the visible/terminal evidence layer: a
withdrawn successor resolves to NO record (never resurrecting its predecessor), a
superseding correction wins over the original, a record whose publication time is
after the visibility bound is not consumed early, and a record with unapproved PAT
provenance is never consumed at all.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from tradehub_research.ops.market_calendar import is_session_day
from tradehub_research.portfolio.prices import (
    _OUTCOME_VISIBILITY_BOUND,
    _action_records,
    _bar_records,
    _cumulative_adjustments,
    _d,
    _session_key,
    _visible_records,
)
from tradehub_research.validation.horizons import entry_session_for

ENTRY_CONVENTION_OPEN = "next_session_open"
ENTRY_CONVENTION_CLOSE_FALLBACK = "next_session_close_fallback"


def visible_records(db: Any, security_id: str, visibility_bound: str) -> list[dict[str, Any]]:
    """Visible terminal evidence for a security at a bound (one resolution pass).

    Callers that need entry + exit + actions for the SAME window should resolve
    once and pass ``records=`` to the helpers below: chain resolution loads the
    security's whole evidence history, and doing it three times per evaluation is
    pure waste.
    """
    return _visible_records(db, security_id, visibility_bound)


def bar_open(bar: dict[str, Any]) -> Decimal | None:
    return _d(bar["structured_fields"].get("open"))


def bar_close(bar: dict[str, Any]) -> Decimal | None:
    return _d(bar["structured_fields"].get("close"))


def _entry_bar_from(
    records: list[dict[str, Any]], after_ts: str, visibility_bound: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Canonical bar ON the market session the calendar expects next, from records."""
    expected_session = entry_session_for(after_ts)
    for bar in _bar_records(records, visibility_bound):
        session = _session_key(bar)
        if session == expected_session:
            return bar, session
        if session > expected_session:
            return None, None
    return None, None


def entry_for(
    db: Any,
    security_id: str,
    observation_date: str,
    *,
    visibility_bound: str = _OUTCOME_VISIBILITY_BOUND,
    records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str | None, Decimal | None, str | None]:
    """Canonical entry for an outcome window.

    Returns ``(bar, entry_session, entry_price, entry_convention)``. ``bar`` /
    ``entry_session`` are ``None`` when the calendar's expected entry session has
    no visible terminal bar; ``entry_price`` is ``None`` when no usable (positive)
    price exists.
    """
    bar, session = _entry_bar_from(
        records if records is not None else visible_records(db, security_id, visibility_bound),
        observation_date,
        visibility_bound,
    )
    if bar is None or session is None:
        return None, None, None, None
    open_price = bar_open(bar)
    if open_price is not None and open_price > 0:
        return bar, session, open_price, ENTRY_CONVENTION_OPEN
    close_price = bar_close(bar)
    price = close_price if (close_price is not None and close_price > 0) else None
    return bar, session, price, ENTRY_CONVENTION_CLOSE_FALLBACK


def visible_session_bars(
    db: Any,
    security_id: str,
    *,
    after_session: str,
    up_to: str | None = None,
    visibility_bound: str = _OUTCOME_VISIBILITY_BOUND,
    records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Visible, canonical bars on REAL market sessions after ``after_session``.

    Evidence authority: ``_visible_records`` resolves supersession chains (a
    withdrawal resolves to no record), publication visibility, and approved PAT
    provenance; ``_bar_records`` collapses identical same-session duplicates and
    drops a session whose bars conflict. On top of that, a bar dated on a weekend
    or market holiday is not a session and is dropped -- it may never establish an
    entry, an exit, or a session count (live example: METRY's calendar-daily feed).
    """
    cutoff = visibility_bound[:10] if up_to is None else min(visibility_bound[:10], up_to[:10])
    if records is None:
        records = visible_records(db, security_id, visibility_bound)
    bars: list[dict[str, Any]] = []
    for bar in _bar_records(records, visibility_bound):
        session = _session_key(bar)
        if session <= after_session[:10] or session > cutoff:
            continue
        try:
            day = date.fromisoformat(session)
        except ValueError:
            continue
        if not is_session_day(day):
            continue
        bars.append(bar)
    return bars


def outcome_returns(
    db: Any,
    security_id: str,
    *,
    entry_session: str,
    entry_price: Decimal,
    exit_session: str,
    exit_close: Decimal,
    visibility_bound: str = _OUTCOME_VISIBILITY_BOUND,
    records: list[dict[str, Any]] | None = None,
) -> tuple[Decimal | None, Decimal | None]:
    """``(raw_return, total_return)`` for a realized window.

    ``raw_return`` uses unadjusted closes. ``total_return`` applies the cumulative
    split factors and cash dividends whose effective date falls inside the window
    (start-exclusive, end-inclusive) -- the documented Phase-5 convention. Both
    come from the SAME visible action evidence as everything else; an ambiguous
    action chain yields no total return rather than a wrong one.
    """
    if records is None:
        records = visible_records(db, security_id, visibility_bound)
    actions = _action_records(records)
    raw_return = exit_close / entry_price - 1
    adjustments = _cumulative_adjustments(actions, entry_session, exit_session)
    total_return = None
    cum_factor, cum_dividend = adjustments
    if cum_factor is not None and cum_dividend is not None:
        total_return = (exit_close * cum_factor + cum_dividend) / entry_price - 1
    return raw_return, total_return

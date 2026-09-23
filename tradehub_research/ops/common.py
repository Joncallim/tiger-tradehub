"""Ops common: paths, env, and the market-calendar clock.

The collection clock for forward predictions is the LAST COMPLETED US
market session (as_of semantics: a production screen runs on the completed
session's data). The clock is injectable so tests stay deterministic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tradehub_research.ops.market_calendar import expected_latest_session


@dataclass(frozen=True)
class ResearchPaths:
    """The single source of research paths (deployment-aware)."""

    research_dir: Path
    research_db: Path
    experiment_db: Path
    replay_db: Path
    snapshots_dir: Path
    artifacts_dir: Path
    raw_cache: Path

    @property
    def report_dir(self) -> Path:
        return self.research_dir / "reports"


def research_paths() -> ResearchPaths:
    """Resolve paths from env overrides (deployment sets TRADEHUB_RESEARCH_DIR)
    falling back to the in-repo development layout."""
    base = Path(os.environ.get("TRADEHUB_RESEARCH_DIR", "data/research"))
    return ResearchPaths(
        research_dir=base,
        research_db=Path(os.environ.get("TRADEHUB_RESEARCH_DB", base / "research.db")),
        experiment_db=Path(os.environ.get("TRADEHUB_EXPERIMENT_DB", base / "experiment.db")),
        replay_db=Path(os.environ.get("TRADEHUB_REPLAY_DB", base / "validation_replay.db")),
        snapshots_dir=Path(os.environ.get("TRADEHUB_SNAPSHOTS_DIR", base / "snapshots")),
        artifacts_dir=Path(os.environ.get("TRADEHUB_ARTIFACTS_DIR", base / "artifacts")),
        raw_cache=Path(os.environ.get("TRADEHUB_RAW_CACHE", base / "raw")),
    )


# US equity sessions: Monday-Friday. No exchange-holiday calendar is
# embedded -- a missing session simply has no bar (freshness checks report
# it honestly as absent, never backfilled).
WEEKDAYS = frozenset(range(0, 5))


def _previous_weekday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while candidate.weekday() not in WEEKDAYS:
        candidate -= timedelta(days=1)
    return candidate


@dataclass(frozen=True)
class EvaluationClock:
    """The TWO clocks an outcome evaluation needs, kept apart on purpose.

    Conflating them is wrong: ``TiingoEodAdapter.parse`` publishes a session's EOD
    bar at **20:15 America/New_York converted to UTC**, so the US session dated
    2026-09-30 is published at 2026-10-01T00:15:00Z. A bound of
    "2026-09-30T23:59:59Z" would therefore exclude the genuine Sep-30 bar from a
    run that is already past it in real time -- and moving the date by a day is
    not a fix.

    ``now``
        The actual UTC evaluation timestamp. This is the EVIDENCE VISIBILITY
        bound: a record with ``public_available_time <= now`` is visible, and
        nothing published later can be consumed early.
    ``session_cutoff``
        The latest US session whose EOD evidence is expected to exist by ``now``
        -- ``market_calendar.expected_latest_session``, which is exchange-local,
        DST-correct, holiday-aware and uses the same close + 4h15m (20:15 ET)
        publication boundary as the real adapter. THIS is the market-session
        clock: a horizon has elapsed when
        ``required_exit_session <= session_cutoff``.
    ``evaluation_date``
        ``now``'s UTC date, used only as the coarse advisory scheduling gate
        (``outcome_due_date <= evaluation_date``) so that a row can never be
        skipped by a stricter gate than its own advisory date.
    """

    now: datetime
    session_cutoff: date
    evaluation_date: date

    @property
    def visibility_bound(self) -> str:
        """ISO-8601 UTC (``Z``) timestamp for evidence-visibility comparisons."""
        return self.now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def evaluation_clock(now: datetime | None = None) -> EvaluationClock:
    """Build the evaluation clock from an (injectable) UTC timestamp.

    NOTE: ``last_completed_us_session`` below is weekday-only UTC arithmetic with
    no exchange-holiday or time-of-day awareness. It is NOT the authority for
    outcome evaluation -- use this clock.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return EvaluationClock(
        now=moment,
        session_cutoff=expected_latest_session(moment),
        evaluation_date=moment.date(),
    )


def last_completed_us_session(now: datetime | None = None) -> date:
    """The last COMPLETED US equity session (strictly before today).

    EOD data for today is not published until after the close; backfill and
    refresh jobs always operate on the last completed session. Injectable
    ``now`` keeps tests deterministic.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if today.weekday() in WEEKDAYS:
        return _previous_weekday(today)
    # Weekend: the last completed session is Friday.
    return _previous_weekday(today)

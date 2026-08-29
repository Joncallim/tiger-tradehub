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

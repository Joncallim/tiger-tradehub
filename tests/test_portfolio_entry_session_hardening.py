"""Entry-session validity at the HELPER and OUTCOME-BUILDER level.

The forward maturation path enforced the market calendar, but
``portfolio.prices.next_session_on_or_after`` (the research/replay ENTRY
convention used by ``validation.outcome_builder``) was calendar-blind: it returned
the first canonical bar with ``session_date > after_ts``, so a weekend/holiday bar
could become the entry session and every calendar day could count as a session
(live example: ``METRY``, METRO INC./ADR, calendar-daily Tiingo bars).

Both planes must now use ONE entry rule: the first VALID market session strictly
after the observation, from the same authoritative calendar
(``validation.horizons.entry_session_for``). No later-entry substitution, and the
existing canonical duplicate/conflict semantics are preserved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import evaluation_clock
from tradehub_research.ops.market_calendar import is_session_day, next_session
from tradehub_research.portfolio.prices import next_session_on_or_after
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.horizons import entry_session_for, required_exit_session
from tradehub_research.validation.outcome_builder import build_outcome_label


def _night_after(session_date: str) -> datetime:
    """The scheduled 23:45 (+08) = 15:45Z run that can see `session_date`'s EOD.

    The real Tiingo PAT for a US session is 20:15 ET -> UTC (the next UTC day), so
    the first run able to use that session is the following night's.
    """
    return datetime.fromisoformat(f"{session_date}T15:45:00+00:00") + timedelta(days=1)


SECURITY = "sec-1"
OBSERVATION = "2026-05-29T20:15:00Z"  # a Friday
EXPECTED_ENTRY = "2026-06-01"  # the Monday session the calendar expects
HOLIDAY = "2026-06-19"  # Juneteenth (Friday, market closed)


def _sessions_from(start: str, count: int) -> list[str]:
    out: list[str] = []
    day = date.fromisoformat(start)
    while len(out) < count:
        if is_session_day(day):
            out.append(day.isoformat())
        day = day + timedelta(days=1)
    return out


def _next_session(day: str) -> str:
    return next_session(date.fromisoformat(day)).isoformat()


def _setup(
    tmp_path, bars: list[tuple[str, float]], duplicates: list[tuple[str, float]] | None = None
):
    """Research db + experiment snapshot, one security, the given price bars."""
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot VALUES "
            "('snap-1','abc',11,NULL,'{}','h1','/tmp/x','h2','{}','READY','2025-01-01T00:00:00Z')"
        )
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                SECURITY,
                "METRY",
                "US",
                "Metro Inc ADR",
                "Consumer Staples",
                "Grocery",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
    store = EvidenceStore(research_db)
    for i, (day, close) in enumerate(list(bars) + list(duplicates or [])):
        store.insert(
            security_id=SECURITY,
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": "METRY",
                "session_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            },
            extraction_confidence=0.95,
            event_time=f"{day}T00:00:00Z",
            public_available_time=f"{day}T20:15:00Z",
            pat_provenance="source_reported",
            source_record_id=f"rec-{day}-{i}",
        )
    return research_db, experiment_db


def _entry(research_db, observation: str = OBSERVATION):
    with research_db.connect() as conn:
        return next_session_on_or_after(conn, SECURITY, observation)


def _label(experiment_db, research_db, horizon: int = 21) -> dict:
    return build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id=SECURITY,
        observation_date=OBSERVATION,
        horizon_sessions=horizon,
    )


# ------------------------------------------------------------- helper level


def test_helper_selects_the_monday_session_not_the_weekend_bars(tmp_path):
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars = [("2026-05-30", 999.0), ("2026-05-31", 999.0)]
    bars += [(d, 100.0 + i) for i, d in enumerate(sessions)]
    research_db, _ = _setup(tmp_path, bars)
    bar, session = _entry(research_db)
    assert session == EXPECTED_ENTRY, f"expected {EXPECTED_ENTRY}, got {session}"
    assert bar["structured_fields"]["session_date"] == EXPECTED_ENTRY
    assert bar["structured_fields"]["close"] == 100.0, "the Monday close must be the entry"


def test_helper_ignores_a_holiday_dated_bar(tmp_path):
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    assert HOLIDAY not in sessions
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    bars.append((HOLIDAY, 999.0))
    bars.sort()
    research_db, _ = _setup(tmp_path, bars)
    _bar, session = _entry(research_db)
    assert session == EXPECTED_ENTRY


def test_helper_never_substitutes_a_later_session_for_a_missing_entry(tmp_path):
    """Expected Monday has no bar; Tuesday onwards do -> no entry, not Tuesday."""
    sessions = [d for d in _sessions_from(EXPECTED_ENTRY, 23) if d != EXPECTED_ENTRY]
    research_db, _ = _setup(tmp_path, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    bar, session = _entry(research_db)
    assert (bar, session) == (None, None), "the entry must not shift to a later session"


def test_helper_collapses_identical_duplicate_entry_bars(tmp_path):
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    research_db, _ = _setup(tmp_path, bars, duplicates=[(EXPECTED_ENTRY, 100.0)])
    _bar, session = _entry(research_db)
    assert session == EXPECTED_ENTRY


def test_helper_reports_no_entry_on_conflicting_entry_bars(tmp_path):
    """Conflicting same-session bars make the session UNKNOWN (existing rule)."""
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    research_db, _ = _setup(tmp_path, bars, duplicates=[(EXPECTED_ENTRY, 12345.0)])
    bar, session = _entry(research_db)
    assert (bar, session) == (None, None)


# ------------------------------------------------------ outcome builder level


def test_builder_entry_is_the_monday_session(tmp_path):
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars = [("2026-05-30", 999.0), ("2026-05-31", 999.0)]
    bars += [(d, 100.0 + i) for i, d in enumerate(sessions)]
    research_db, experiment_db = _setup(tmp_path, bars)
    label = _label(experiment_db, research_db)
    assert label["entry_session_date"] == EXPECTED_ENTRY, label
    assert label["outcome_status"] == "OBSERVED", label


def test_builder_marks_entry_unavailable_instead_of_substituting(tmp_path):
    sessions = [d for d in _sessions_from(EXPECTED_ENTRY, 23) if d != EXPECTED_ENTRY]
    research_db, experiment_db = _setup(tmp_path, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    label = _label(experiment_db, research_db)
    assert label["outcome_status"] == "ENTRY_UNAVAILABLE", label


def test_builder_counts_the_horizon_in_sessions_not_calendar_days(tmp_path):
    """Weekend/holiday bars are interleaved and must not shorten the horizon."""
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars: list[tuple[str, float]] = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    bars.append((HOLIDAY, 999.0))
    for a, b in zip(sessions, sessions[1:], strict=False):
        day = date.fromisoformat(a) + timedelta(days=1)
        while day.isoformat() < b:
            if not is_session_day(day):
                bars.append((day.isoformat(), 999.0))
            day = day + timedelta(days=1)
    bars.sort()
    research_db, experiment_db = _setup(tmp_path, bars)
    label = _label(experiment_db, research_db)
    assert label["entry_session_date"] == EXPECTED_ENTRY
    assert label["exit_session_date"] == required_exit_session(EXPECTED_ENTRY, 21), label


def test_builder_never_emits_a_non_session_date(tmp_path):
    """Invariant across horizons: emitted entry/exit dates are real sessions."""
    sessions = _sessions_from(EXPECTED_ENTRY, 64)
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    bars += [("2026-05-30", 999.0), ("2026-05-31", 999.0), (HOLIDAY, 999.0)]
    bars.sort()
    research_db, experiment_db = _setup(tmp_path, bars)
    emitted = 0
    for horizon in (21, 63):
        label = _label(experiment_db, research_db, horizon=horizon)
        assert label["entry_session_date"] == entry_session_for(OBSERVATION[:10])
        assert is_session_day(date.fromisoformat(label["entry_session_date"]))
        if label["exit_session_date"]:
            assert is_session_day(date.fromisoformat(label["exit_session_date"])), label
            assert label["exit_session_date"] == required_exit_session(EXPECTED_ENTRY, horizon)
            emitted += 1
    assert emitted == 2


def test_both_planes_use_the_same_entry_rule(tmp_path):
    """The forward and research planes must agree on the entry session."""
    from tradehub_research.ops.outcome_maturation import _evaluate

    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars = [("2026-05-30", 999.0), ("2026-05-31", 999.0)]
    bars += [(d, 100.0 + i) for i, d in enumerate(sessions)]
    research_db, experiment_db = _setup(tmp_path, bars)
    _bar, helper_session = _entry(research_db)
    forward = _evaluate(
        research_db,
        security_id=SECURITY,
        as_of=OBSERVATION[:10],
        horizon_sessions=21,
        clock=evaluation_clock(_night_after(required_exit_session(EXPECTED_ENTRY, 21))),
    )
    assert helper_session == forward["entry_session_date"] == EXPECTED_ENTRY
    assert forward["status"] == "OBSERVED"
    assert forward["exit_session_date"] == required_exit_session(EXPECTED_ENTRY, 21)

"""AWAITING_ENTRY_BAR is exposed operationally and NEVER terminalized by time.

``forward_outcome`` is append-only with one row per prediction, so converting a
missing entry bar into ``CENSORED``/``DELISTING`` merely because time passed would
make a repairable data-quality gap irreversible. Instead the state is surfaced in
``forward_health`` (total, oldest as_of, distinct securities, age in sessions and
days, recoverability) and rendered in the daily report when non-zero.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session
from tradehub_research.ops.health import forward_health
from tradehub_research.ops.market_calendar import is_session_day
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.horizons import entry_session_for, required_exit_session


def _night_after(session_date: str) -> datetime:
    """The scheduled 23:45 (+08) = 15:45Z run that can see `session_date`'s EOD.

    The real Tiingo PAT for a US session is 20:15 ET -> UTC (the next UTC day), so
    the first run able to use that session is the following night's.
    """
    return datetime.fromisoformat(f"{session_date}T15:45:00+00:00") + timedelta(days=1)


SECURITY = "S1"
AS_OF = "2026-05-29"  # Friday, expected entry Monday 2026-06-01
EXPECTED_ENTRY = "2026-06-01"


def _paths(tmp_path) -> ResearchPaths:
    return ResearchPaths(
        research_dir=tmp_path / "research",
        research_db=tmp_path / "research.db",
        experiment_db=tmp_path / "experiment.db",
        replay_db=tmp_path / "replay.db",
        snapshots_dir=tmp_path / "snapshots",
        artifacts_dir=tmp_path / "artifacts",
        raw_cache=tmp_path / "raw",
    )


def _sessions_from(start: str, count: int) -> list[str]:
    out: list[str] = []
    day = date.fromisoformat(start)
    while len(out) < count:
        if is_session_day(day):
            out.append(day.isoformat())
        day = day + timedelta(days=1)
    return out


def _seed(tmp_path, *, session_dates: list[str], delisted_at: str | None = None, ticker="TST"):
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    exp = ExperimentDB(tmp_path / "experiment.db", 5000)
    exp.migrate()
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                SECURITY,
                ticker,
                "US",
                "Test Co",
                "Technology",
                "HW",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                delisted_at,
            ),
        )
    store = EvidenceStore(research_db)
    for day in session_dates:
        store.insert(
            security_id=SECURITY,
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": ticker,
                "session_date": day,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1000,
            },
            extraction_confidence=0.9,
            event_time=f"{day}T00:00:00Z",
            public_available_time=f"{day}T20:15:00Z",
            pat_provenance="source_reported",
            source_record_id=f"{ticker}:{day}",
        )
    return _paths(tmp_path), research_db, exp


def _prediction(exp: ExperimentDB, *, as_of: str = AS_OF, horizon: int = 21, pip: str = "P1"):
    from tradehub_research.validation.forward_collector import _outcome_due_date

    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_prediction(prediction_id, security_id, as_of, variant_name,"
            " score_value, state, screen_passed, sufficient_data, raw_features_hash, config_hash,"
            " evidence_ids_json, horizon_sessions, outcome_due_date, created_at, provenance)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pip,
                SECURITY,
                as_of,
                "production",
                0.5,
                "CA",
                1,
                1,
                "h",
                "c",
                "[]",
                horizon,
                _outcome_due_date(as_of, horizon),
                f"{as_of}T20:15:00Z",
                "production",
            ),
        )
        conn.commit()


def _run_maturation(exp, research_db, paths, collection: date):
    return mature_due_outcomes(
        settings=ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000),
        experiment_db=exp,
        paths=paths,
        now=_night_after(collection),
    )


def _outcome_count(exp: ExperimentDB) -> int:
    with exp.connect(read_only=True) as conn:
        return conn.execute("SELECT COUNT(*) FROM forward_outcome").fetchone()[0]


def test_missing_entry_bar_is_exposed_with_age_and_recoverability(tmp_path):
    """The expected Monday bar is absent; the state must be visible, not silent."""
    sessions = [d for d in _sessions_from(EXPECTED_ENTRY, 22) if d != EXPECTED_ENTRY]
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    _prediction(exp)
    due = date.fromisoformat(required_exit_session(EXPECTED_ENTRY, 21))

    health = forward_health(experiment_db=exp, paths=paths, now=_night_after(due))
    awaiting = health["awaiting_entry"]
    assert awaiting["total"] == 1
    assert awaiting["distinct_securities"] == 1
    assert awaiting["oldest_as_of"] == AS_OF
    assert awaiting["age_days"] and awaiting["age_days"] > 0
    assert awaiting["age_sessions"] and awaiting["age_sessions"] > 0
    assert awaiting["recoverable"]["securities"] == 1
    assert awaiting["unrecoverable"] == []
    assert "never terminalised" in awaiting["note"]


def test_missing_entry_bar_is_not_terminalised_when_time_passes(tmp_path):
    """No timeout may convert a missing entry bar into a permanent status."""
    sessions = [d for d in _sessions_from(EXPECTED_ENTRY, 22) if d != EXPECTED_ENTRY]
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    _prediction(exp)

    # Run far past the horizon AND past the data grace window.
    for months in (0, 3, 12):
        collection = date.fromisoformat(required_exit_session(EXPECTED_ENTRY, 21)) + timedelta(
            days=30 * months
        )
        summary = _run_maturation(exp, research_db, paths, collection)
        assert summary["matured"] == {}, f"nothing may be terminalised: {summary}"
        assert summary["awaiting"]["AWAITING_ENTRY_BAR"] == 1
    assert _outcome_count(exp) == 0, "no irreversible row may be written"


def test_a_delisted_security_is_reported_unrecoverable(tmp_path):
    """A name that delisted AFTER the window cannot supply the entry bar: the

    gap is surfaced as unrecoverable (positive evidence: delisting) without the
    maturation inventing a timeout-based terminal label.
    """
    paths, research_db, exp = _seed(tmp_path, session_dates=[], delisted_at="2026-08-03")
    _prediction(exp)
    due = date.fromisoformat(required_exit_session(EXPECTED_ENTRY, 21))

    health = forward_health(experiment_db=exp, paths=paths, now=_night_after(due))
    awaiting = health["awaiting_entry"]
    assert awaiting["total"] == 1
    assert awaiting["recoverable"]["securities"] == 0
    assert [u["reason"] for u in awaiting["unrecoverable"]] == ["delisted"]
    assert awaiting["unrecoverable"][0]["ticker"] == "TST"


def test_health_reports_zero_when_there_is_nothing_awaiting(tmp_path):
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    _prediction(exp)
    due = date.fromisoformat(required_exit_session(EXPECTED_ENTRY, 21))

    health = forward_health(experiment_db=exp, paths=paths, now=_night_after(due))
    assert health["awaiting_entry"]["total"] == 0
    assert health["awaiting_entry"]["unrecoverable"] == []


def test_report_renders_the_waiting_line_only_when_present(tmp_path):
    from tradehub_research.validation.reporting import render_daily_report

    base = {"predictions": 5, "new_matured": 0, "system_health": "healthy"}
    assert "Awaiting entry bar" not in render_daily_report(dict(base))
    rendered = render_daily_report(
        {
            **base,
            "awaiting_entry": {
                "total": 12,
                "oldest_as_of": AS_OF,
                "distinct_securities": 3,
                "age_sessions": 18,
                "age_days": 26,
                "recoverable": {"securities": 2, "predictions": 10},
                "unrecoverable": [{"security_id": "X", "reason": "delisted"}],
            },
        }
    )
    assert "Awaiting entry bar: 12" in rendered
    assert "2 recoverable / 1 unrecoverable" in rendered


def test_the_live_clock_is_used_by_default(tmp_path):
    """A sanity check that health defaults to the last completed US session."""
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    _prediction(exp)
    health = forward_health(experiment_db=exp, paths=paths)
    assert health["awaiting_entry"]["total"] == 0  # not due yet by default
    assert entry_session_for(AS_OF) == EXPECTED_ENTRY
    assert last_completed_us_session() > date(2026, 9, 1)

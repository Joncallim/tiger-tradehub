"""Forward-outcome maturation: horizon correctness and honest missing data.

Regression tests for the pre-fix defect: ``_realized_return`` took the LATEST
bar at or before a calendar target (entry + a hard-coded 40/105/210/420 days),
so a prediction could be appended ``OBSERVED`` before its session horizon had
actually completed -- and a late run could observe a horizon that was too long.

Authoritative semantics asserted here:

* a horizon is **N completed trading sessions after the entry session**;
* ``OBSERVED`` requires exactly that many sessions to exist;
* fewer sessions than the horizon is **pending** (no row), never ``OBSERVED``
  and never a fabricated horizon;
* weekends and market holidays are not sessions;
* bars *after* the required exit session never move the selected exit;
* ``forward_prediction`` rows are never modified.

Every test is deterministic: the session calendar decides the dates, no test
hard-codes holiday knowledge, and both databases are seeded locally.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths
from tradehub_research.ops.market_calendar import count_sessions, next_session
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import _outcome_due_date

VALID_STATUSES = {"OBSERVED", "DELISTING_OUTCOME_UNKNOWN", "CENSORED_INSUFFICIENT_HORIZON"}


def _night_after(session_date: str) -> datetime:
    """The scheduled 23:45 (+08) = 15:45Z run that can see `session_date`'s EOD.

    The real Tiingo PAT for a US session is 20:15 ET -> UTC (the next UTC day), so
    the first run able to use that session is the following night's.
    """
    return datetime.fromisoformat(f"{session_date}T15:45:00+00:00") + timedelta(days=1)


SECURITY = "S1"
TICKER = "TEST"
# A historical anchor so every bar is realized (the evidence store refuses
# public_available_time in the future). Friday -> entry is the next session.
AS_OF = "2026-05-29"


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


def _seed(tmp_path, *, session_dates: list[str], delisted_at: str | None = None):
    """One security with exactly the given price-bar sessions."""
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    store = EvidenceStore(research_db)
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                SECURITY,
                TICKER,
                "US",
                "Test Co",
                "Technology",
                "HW",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                delisted_at,
            ),
        )
    for day in session_dates:
        store.insert(
            security_id=SECURITY,
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": TICKER,
                "session_date": day,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0 + session_dates.index(day),  # distinct per session
                "volume": 1000,
            },
            extraction_confidence=1.0,
            event_time=f"{day}T00:00:00Z",
            public_available_time=f"{day}T20:15:00Z",
            pat_provenance="derived_from_index",
            source_record_id=f"{SECURITY}:{day}:bar",
        )
    exp = ExperimentDB(tmp_path / "experiment.db")
    exp.migrate()
    return _paths(tmp_path), research_db, exp


def _bars(count: int) -> list[str]:
    """The first ``count`` sessions strictly after AS_OF (``_bars(1)`` == the entry).

    ``_bars(N + 1)`` means N sessions realized AFTER the entry session.
    """
    day = next_session(date.fromisoformat(AS_OF))
    sessions = []
    for _ in range(count):
        sessions.append(day.isoformat())
        day = next_session(day)
    return sessions


def _entry_session() -> str:
    return _bars(1)[0]


def _insert_prediction(
    exp: ExperimentDB,
    *,
    horizon: int,
    due: str,
    provenance: str = "production",
    variant: str = "production",
    as_of: str = AS_OF,
) -> str:
    """Insert a prediction row directly (the API is not what is under test)."""
    prediction_id = f"pred-{horizon}-{due}-{provenance}"
    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_prediction("
            "prediction_id, security_id, as_of, variant_name, score_value, state, "
            "screen_passed, sufficient_data, raw_features_hash, config_hash, "
            "evidence_ids_json, horizon_sessions, outcome_due_date, created_at, provenance"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                prediction_id,
                SECURITY,
                as_of,
                variant,
                1.0,
                "ok",
                1,
                1,
                "rawhash",
                "confighash",
                "[]",
                horizon,
                due,
                f"{as_of}T20:15:00Z",
                provenance,
            ),
        )
    return prediction_id


def _outcomes(exp: ExperimentDB) -> list[dict]:
    with exp.connect(read_only=True) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT prediction_id, outcome_status, total_return, entry_session_date, "
                "exit_session_date FROM forward_outcome ORDER BY prediction_id"
            )
        ]


def _settings(research_db: ResearchDB) -> ResearchSettings:
    return ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)


# ---------------------------------------------------------------------------
# Live shape: due by the written calendar date, immature by sessions
# ---------------------------------------------------------------------------
def test_twenty_sessions_cannot_be_observed(tmp_path):
    """THE live-shape regression.

    The written ``outcome_due_date`` has arrived, but only 20 of the required 21
    sessions have completed. Pre-fix this appended ``OBSERVED`` (taking the
    latest available bar); a prediction may only become OBSERVED once the
    horizon has genuinely matured.
    """
    paths, research_db, exp = _seed(tmp_path, session_dates=_bars(21))
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    summary = mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )

    assert summary["due"] == 1
    assert _outcomes(exp) == [], "an immature horizon must not be materialized"
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1


def test_the_same_prediction_becomes_observed_once_the_horizon_completes(tmp_path):
    """Pending is retryable: the 21st session is enough, and no earlier."""
    sessions = _bars(22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )

    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0]["outcome_status"] == "OBSERVED"
    # Exit is EXACTLY the 21st session after entry -- not the latest bar.
    assert rows[0]["entry_session_date"] == _entry_session()
    # 22 bars = entry + 21 sessions; the exit is the 21st of those sessions.
    assert rows[0]["exit_session_date"] == sessions[21]
    assert rows[0]["total_return"] is not None


def test_weekends_and_holidays_are_not_sessions(tmp_path):
    """Session counting must skip weekends and market holidays.

    2026-06-19 is Juneteenth: a 21-session horizon from the 2026-06-01 entry
    therefore lands later in the calendar than 21 plain days would suggest.
    """
    sessions = _bars(22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(sessions[-1]),
    )

    row = _outcomes(exp)[0]
    exit_session = date.fromisoformat(row["exit_session_date"])
    entry_session = date.fromisoformat(row["entry_session_date"])
    # inclusive=False: the entry session is not one of the 21 counted after it.
    assert count_sessions(entry_session, exit_session, inclusive=False) == 21, (
        "exactly 21 sessions after entry"
    )
    assert (exit_session - entry_session).days > 21, "…which spans more than 21 days"
    # No weekend or holiday was counted as a session.
    assert exit_session.weekday() < 5


def test_bars_after_the_horizon_do_not_move_the_exit(tmp_path):
    """A late run must not stretch the horizon onto later bars."""
    sessions = _bars(22)
    extra = _bars(41)[22:]
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions + extra)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(extra[-1]),
    )

    row = _outcomes(exp)[0]
    assert row["exit_session_date"] == sessions[21], "exit stays the 21st post-entry session"
    assert row["exit_session_date"] != extra[-1]


@pytest.mark.parametrize("horizon", [21, 63])
def test_longer_horizons_use_the_same_rule(tmp_path, horizon):
    """One session short is pending; exactly the horizon is OBSERVED."""
    short = _bars(horizon)
    due = _outcome_due_date(AS_OF, horizon)

    paths, research_db, exp = _seed(tmp_path / "short", session_dates=short)
    _insert_prediction(exp, horizon=horizon, due=due)
    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )
    assert _outcomes(exp) == [], f"h{horizon}: {horizon - 1} sessions is immature"

    full = _bars(horizon + 1)
    paths, research_db, exp = _seed(tmp_path / "full", session_dates=full)
    _insert_prediction(exp, horizon=horizon, due=due)
    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )
    rows = _outcomes(exp)
    assert len(rows) == 1 and rows[0]["outcome_status"] == "OBSERVED"
    assert rows[0]["exit_session_date"] == full[horizon]


# ---------------------------------------------------------------------------
# Missing data must stay honest (and must never be an invalid status)
# ---------------------------------------------------------------------------
def test_no_entry_session_is_pending_not_a_bogus_status(tmp_path):
    """No bars at all after as_of: nothing may be appended yet.

    The pre-fix code returned the status ``ENTRY_UNAVAILABLE``, which the
    forward_outcome CHECK constraint does not allow -- the insert would raise
    IntegrityError and abort the run. Pending (no row) is the honest state while
    the data may still arrive.
    """
    paths, research_db, exp = _seed(tmp_path, session_dates=["2026-05-26", "2026-05-27"])
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    summary = mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )

    assert _outcomes(exp) == []
    assert summary["awaiting"]["AWAITING_ENTRY_BAR"] == 1


def test_every_appended_status_is_in_the_schema_enum(tmp_path):
    """Defence in depth: a status outside the CHECK enum would abort the run."""
    sessions = _bars(22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)
    # A second, unresolvable name (no bars) in the same run.
    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_prediction("
            "prediction_id, security_id, as_of, variant_name, score_value, state, "
            "screen_passed, sufficient_data, raw_features_hash, config_hash, "
            "evidence_ids_json, horizon_sessions, outcome_due_date, created_at, provenance"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "pred-nobars",
                "S-NOBARS",
                AS_OF,
                "production",
                0.0,
                "ok",
                0,
                0,
                "rawhash",
                "confighash",
                "[]",
                21,
                due,
                f"{AS_OF}T20:15:00Z",
                "production",
            ),
        )

    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )

    for row in _outcomes(exp):
        assert row["outcome_status"] in VALID_STATUSES


def test_a_delisted_name_is_recorded_not_dropped(tmp_path):
    """A name that stopped trading mid-horizon gets the honest terminal label."""
    sessions = _bars(6)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions, delisted_at=sessions[-1])
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )

    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0]["outcome_status"] == "DELISTING_OUTCOME_UNKNOWN"
    assert rows[0]["total_return"] is None


# ---------------------------------------------------------------------------
# Immutability and scoping
# ---------------------------------------------------------------------------
def test_prediction_rows_are_never_modified(tmp_path):
    """Maturation appends; the immutable prediction row is untouched."""
    sessions = _bars(22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    prediction_id = _insert_prediction(exp, horizon=21, due=due)

    def snapshot() -> tuple:
        with exp.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT * FROM forward_prediction WHERE prediction_id=?", (prediction_id,)
            ).fetchone()
        return tuple(row)

    before = snapshot()
    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )
    assert snapshot() == before
    # The immutability trigger raises sqlite3.IntegrityError (RAISE(ABORT)).
    with pytest.raises(sqlite3.IntegrityError):
        with exp.connect() as conn:
            conn.execute(
                "UPDATE forward_prediction SET outcome_due_date='1999-01-01' WHERE prediction_id=?",
                (prediction_id,),
            )


def test_replay_bootstrap_predictions_are_out_of_scope(tmp_path):
    """Only provenance='production' rows are matured."""
    sessions = _bars(22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due, provenance="replay_bootstrap")

    summary = mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(due),
    )
    assert summary["due"] == 0
    assert _outcomes(exp) == []


def test_second_run_does_not_double_append(tmp_path):
    """UNIQUE(prediction_id): a re-run is a no-op once the outcome exists."""
    sessions = _bars(22)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    for _ in range(2):
        mature_due_outcomes(
            settings=_settings(research_db),
            experiment_db=exp,
            paths=paths,
            now=_night_after(due),
        )
    assert len(_outcomes(exp)) == 1


def test_an_immature_horizon_is_not_censored_prematurely(tmp_path):
    """Censoring is permanent (append-only + UNIQUE), so it must wait.

    One session short, with the collection date still inside the honest data
    grace: the row must stay pending so a later run can still OBSERVE it.
    """
    sessions = _bars(21)
    paths, research_db, exp = _seed(tmp_path, session_dates=sessions)
    due = _outcome_due_date(AS_OF, 21)
    _insert_prediction(exp, horizon=21, due=due)

    mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(sessions[-1]) + timedelta(days=1),
    )
    assert _outcomes(exp) == [], "a censored row here could never be observed later"

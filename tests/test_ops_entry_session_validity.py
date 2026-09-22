"""Entry-session validity: a non-session bar must never establish a horizon.

Live defect (2026-09-22): security ``METRY`` (METRO INC./ADR) carries
CALENDAR-daily Tiingo bars -- 194 of its 621 bars sit on weekends or market
holidays (2026-08-29 Sat, 2026-08-30 Sun, 2026-09-07 Labor Day, ...). The bar
selector took the first bar whose ``session_date > as_of`` without asking the
market calendar, so a Saturday bar could become the entry session and every
calendar day could count as a session.

Invariants enforced here:
  * entry session = the first VALID market session strictly after ``as_of``;
  * the entry bar must be dated on exactly that session;
  * weekend/holiday bars never establish entry, exit, session count or timing;
  * a missing expected-entry bar is NOT replaced by a later bar;
  * the existing canonical rule holds: identical duplicates collapse,
    conflicting same-session bars make that session UNKNOWN.
"""

from __future__ import annotations

from datetime import date, timedelta

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths
from tradehub_research.ops.market_calendar import is_session_day, next_session
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.horizons import entry_session_for, required_exit_session

SECURITY = "S1"
TICKER = "METRY"
AS_OF = "2026-05-29"  # Friday
EXPECTED_ENTRY = "2026-06-01"  # the Monday session the calendar expects
HOLIDAY = "2026-06-19"  # Juneteenth (Friday, market closed)


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


def _settings(research_db: ResearchDB) -> ResearchSettings:
    return ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)


def _sessions_from(start: str, count: int) -> list[str]:
    """``count`` valid market sessions starting at ``start`` (inclusive)."""
    out: list[str] = []
    day = date.fromisoformat(start)
    while len(out) < count:
        if is_session_day(day):
            out.append(day.isoformat())
        day = day + timedelta(days=1)
    return out


def _calendar_days_between(start: str, end: str) -> list[str]:
    """Non-session calendar days strictly between two dates (weekend/holiday)."""
    out: list[str] = []
    day = date.fromisoformat(start) + timedelta(days=1)
    last = date.fromisoformat(end)
    while day < last:
        if not is_session_day(day):
            out.append(day.isoformat())
        day = day + timedelta(days=1)
    return out


def _seed(
    tmp_path, bars: list[tuple[str, float]], duplicates: list[tuple[str, float]] | None = None
):
    """One security with exactly the given price bars (any calendar date)."""
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    exp = ExperimentDB(tmp_path / "experiment.db", 5000)
    exp.migrate()
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
                "Metro Inc ADR",
                "Consumer Staples",
                "Grocery",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
    for day, close in list(bars) + list(duplicates or []):
        store.insert(
            security_id=SECURITY,
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": TICKER,
                "session_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000,
            },
            extraction_confidence=0.9,
            event_time=f"{day}T00:00:00Z",
            public_available_time=f"{next_session(date.fromisoformat(day)).isoformat()}T00:15:00Z",
            pat_provenance="source_reported",
            source_record_id=f"{TICKER}:{day}:price_bar:{close}:{len(duplicates or [])}",
        )
    return _paths(tmp_path), research_db, exp


def _prediction(
    exp: ExperimentDB, *, as_of: str = AS_OF, horizon: int = 21, pip: str = "P1"
) -> str:
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
    return pip


def _run(
    exp: ExperimentDB, research_db: ResearchDB, paths: ResearchPaths, collection_date: str
) -> dict:
    return mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        collection_date=date.fromisoformat(collection_date),
    )


def _outcomes(exp: ExperimentDB) -> list[tuple]:
    with exp.connect(read_only=True) as conn:
        return conn.execute(
            "SELECT prediction_id, outcome_status, total_return, entry_session_date,"
            "       exit_session_date FROM forward_outcome"
        ).fetchall()


# --------------------------------------------------------------------- entry


def test_friday_observation_enters_on_the_monday_session(tmp_path):
    bars = [(d, 100.0 + i) for i, d in enumerate(_sessions_from(EXPECTED_ENTRY, 22))]
    paths, rdb, exp = _seed(tmp_path, bars)
    _prediction(exp)
    _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    rows = _outcomes(exp)
    assert len(rows) == 1, f"expected one outcome, got {rows}"
    assert rows[0][3] == EXPECTED_ENTRY, f"entry must be Monday, got {rows[0][3]}"
    assert rows[0][4] == required_exit_session(EXPECTED_ENTRY, 21)


def test_a_saturday_bar_never_becomes_the_entry_session(tmp_path):
    """The live METRY shape: a Saturday bar exists and must be ignored."""
    bars = [("2026-05-30", 999.0)]
    bars += [(d, 100.0 + i) for i, d in enumerate(_sessions_from(EXPECTED_ENTRY, 22))]
    paths, rdb, exp = _seed(tmp_path, bars)
    _prediction(exp)
    _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0][3] == EXPECTED_ENTRY, f"a Saturday must never be the entry: {rows[0][3]}"
    assert rows[0][2] == 121.0 / 100.0 - 1.0, "the Saturday close must not be the entry price"


def test_a_sunday_bar_is_ignored(tmp_path):
    bars = [("2026-05-31", 999.0)]
    bars += [(d, 100.0 + i) for i, d in enumerate(_sessions_from(EXPECTED_ENTRY, 22))]
    paths, rdb, exp = _seed(tmp_path, bars)
    _prediction(exp)
    _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0][3] == EXPECTED_ENTRY
    assert rows[0][2] == 121.0 / 100.0 - 1.0


def test_a_market_holiday_bar_is_ignored_and_does_not_shift_the_horizon(tmp_path):
    """Juneteenth (2026-06-19) is a holiday: its bar is not a session."""
    assert not is_session_day(date.fromisoformat(HOLIDAY))
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    assert HOLIDAY not in sessions
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    bars.append((HOLIDAY, 999.0))
    bars.sort()
    paths, rdb, exp = _seed(tmp_path, bars)
    _prediction(exp)
    _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0][3] == EXPECTED_ENTRY
    assert rows[0][4] == required_exit_session(EXPECTED_ENTRY, 21)
    assert rows[0][2] == 121.0 / 100.0 - 1.0, "the holiday bar must not count as a session"


def test_a_missing_expected_entry_bar_is_not_replaced_by_a_later_bar(tmp_path):
    """Expected Monday has no bar; Tuesday onwards do. Never shift the entry."""
    sessions = _sessions_from(EXPECTED_ENTRY, 23)
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions) if d != EXPECTED_ENTRY]
    paths, rdb, exp = _seed(tmp_path, bars)
    _prediction(exp)
    summary = _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    assert _outcomes(exp) == [], "a later bar must not be manufactured as the entry"
    assert not summary.get("matured"), f"nothing may mature: {summary}"


def test_identical_duplicate_entry_bars_collapse(tmp_path):
    bars = [(d, 100.0 + i) for i, d in enumerate(_sessions_from(EXPECTED_ENTRY, 22))]
    paths, rdb, exp = _seed(tmp_path, bars, duplicates=[(EXPECTED_ENTRY, 100.0)])
    _prediction(exp)
    _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0][3] == EXPECTED_ENTRY


def test_conflicting_entry_bars_make_the_session_unknown(tmp_path):
    """Conflicting same-session bars -> UNKNOWN -> honest pending, never a guess."""
    bars = [(d, 100.0 + i) for i, d in enumerate(_sessions_from(EXPECTED_ENTRY, 22))]
    paths, rdb, exp = _seed(tmp_path, bars, duplicates=[(EXPECTED_ENTRY, 12345.0)])
    _prediction(exp)
    summary = _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    assert _outcomes(exp) == [], "an UNKNOWN session must not be guessed"
    assert not summary.get("matured"), f"nothing may mature: {summary}"


def test_the_horizon_is_counted_from_valid_sessions_only(tmp_path):
    """Every weekend/holiday bar interleaved; the exit is still the 21st session."""
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars: list[tuple[str, float]] = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    for a, b in zip(sessions, sessions[1:], strict=False):
        bars += [(d, 999.0) for d in _calendar_days_between(a, b)]
    bars.sort()
    paths, rdb, exp = _seed(tmp_path, bars)
    _prediction(exp)
    _run(exp, rdb, paths, required_exit_session(EXPECTED_ENTRY, 21))
    rows = _outcomes(exp)
    assert len(rows) == 1
    assert rows[0][3] == EXPECTED_ENTRY
    assert rows[0][4] == required_exit_session(EXPECTED_ENTRY, 21)
    assert rows[0][2] == 121.0 / 100.0 - 1.0


def test_every_emitted_session_is_a_valid_market_session(tmp_path):
    """Invariant: nothing the maturation emits may be dated on a closed day, and
    every emitted entry must be the calendar's expected entry session."""
    sessions = _sessions_from(EXPECTED_ENTRY, 22)
    bars = [(d, 100.0 + i) for i, d in enumerate(sessions)]
    bars += [("2026-05-30", 999.0), ("2026-05-31", 999.0), (HOLIDAY, 999.0)]
    bars.sort()
    paths, rdb, exp = _seed(tmp_path, bars)
    for i, horizon in enumerate((21, 63, 126, 252)):
        _prediction(exp, horizon=horizon, pip=f"P{i}")
    _run(exp, rdb, paths, "2027-06-01")
    emitted = _outcomes(exp)
    assert emitted, "expected at least one outcome"
    with exp.connect(read_only=True) as conn:
        as_of_by_prediction = {
            row["prediction_id"]: str(row["as_of"])[:10]
            for row in conn.execute("SELECT prediction_id, as_of FROM forward_prediction")
        }
    for prediction_id, _status, _ret, entry, exit_ in emitted:
        for value in (entry, exit_):
            if value is None:
                continue
            assert is_session_day(date.fromisoformat(value)), (
                f"{prediction_id} emitted a non-session date {value}"
            )
        assert entry == entry_session_for(as_of_by_prediction[prediction_id]), (
            f"{prediction_id} entry {entry} is not the calendar's expected entry "
            f"for as_of {as_of_by_prediction[prediction_id]}"
        )


def test_entry_bar_present_with_no_bars_after_it_is_honest(tmp_path):
    """Regression (found live): one bar, exactly on the expected entry session,

    and nothing after it. Before the fix this crashed with an IndexError on an
    empty post-entry list. Behaviour must be honest instead:
      * before the due gate: nothing happens at all;
      * once the horizon has elapsed with no realized exit bar, the prediction
        stays PENDING (AWAITING_EXIT_BAR) -- never a permanent label, because the
        bar may still be ingested;
      * when the required exit bar is backfilled, it heals into OBSERVED.
    """
    paths, rdb, exp = _seed(tmp_path, [(EXPECTED_ENTRY, 100.0)])
    _prediction(exp)
    exit_session = required_exit_session(EXPECTED_ENTRY, 21)

    # Not due yet: the written due date is the session-exact maturity.
    early = next_session(date.fromisoformat(EXPECTED_ENTRY)).isoformat()
    summary = _run(exp, rdb, paths, early)
    assert _outcomes(exp) == []
    assert summary["due"] == 0

    # Due, horizon elapsed, no realized bars -> pending, no permanent row.
    summary = _run(exp, rdb, paths, exit_session)
    assert _outcomes(exp) == [], summary
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1

    # The gap heals once the required exit bar exists.
    from tradehub_research.evidence import EvidenceStore

    rdb2 = ResearchDB(tmp_path / "research.db")
    EvidenceStore(rdb2).insert(
        security_id="S1",
        source_id="tiingo_eod",
        structured_fields={
            "record_type": "price_bar",
            "provider_ticker": TICKER,
            "session_date": exit_session,
            "open": 150.0,
            "high": 150.0,
            "low": 150.0,
            "close": 150.0,
            "volume": 1000,
        },
        extraction_confidence=0.9,
        event_time=f"{exit_session}T00:00:00Z",
        public_available_time=f"{next_session(date.fromisoformat(exit_session)).isoformat()}T00:15:00Z",
        pat_provenance="source_reported",
        source_record_id=f"{TICKER}:{exit_session}:backfill",
    )
    summary = _run(exp, rdb2, paths, exit_session)
    rows = _outcomes(exp)
    assert len(rows) == 1, summary
    assert rows[0][1] == "OBSERVED"
    assert rows[0][4] == exit_session

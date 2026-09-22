"""Live forward ledger vs frozen research snapshot: who may write a terminal label.

Policy under test
-----------------
``forward_outcome`` is append-only with ``UNIQUE(prediction_id)``: one outcome per
prediction, forever. So on the LIVE ledger a permanent non-OBSERVED label is only
written on positive evidence that the outcome can never be recovered (a verified
delisting at/before the session the horizon needs). A missing bar -- entry or exit
-- is a DATA-QUALITY gap: it stays pending (no row) and is retried, because a later
ingestion/backfill can still produce the genuine OBSERVED outcome.

``validation/outcome_builder.py`` labels a FROZEN dataset snapshot whose
observation boundary is final; there, fewer realized sessions than the horizon is
terminally ``CENSORED_INSUFFICIENT_HORIZON``, and that contract is unchanged.
"""

from __future__ import annotations

from datetime import date, timedelta

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session
from tradehub_research.ops.health import forward_health
from tradehub_research.ops.market_calendar import is_session_day
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.horizons import entry_session_for, required_exit_session

SECURITY = "S1"
TICKER = "TST"
AS_OF = "2026-05-29"  # Friday -> expected entry Monday 2026-06-01
EXPECTED_ENTRY = "2026-06-01"
EXIT_SESSION = required_exit_session(EXPECTED_ENTRY, 21)


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
    out: list[str] = []
    day = date.fromisoformat(start)
    while len(out) < count:
        if is_session_day(day):
            out.append(day.isoformat())
        day = day + timedelta(days=1)
    return out


def _next_session(day: str) -> str:
    from tradehub_research.ops.market_calendar import next_session

    return next_session(date.fromisoformat(day)).isoformat()


def _seed(tmp_path, *, delisted_at: str | None = None):
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
    return _paths(tmp_path), research_db, exp


def _insert_bar(research_db: ResearchDB, day: str, close: float, *, tag: str = ""):
    EvidenceStore(research_db).insert(
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
        public_available_time=f"{_next_session(day)}T00:15:00Z",
        pat_provenance="source_reported",
        source_record_id=f"{TICKER}:{day}:{close}{tag}",
    )


def _seed_bars(research_db: ResearchDB, bars: list[tuple[str, float]]):
    for day, close in bars:
        _insert_bar(research_db, day, close)


def _prediction(exp: ExperimentDB, *, horizon: int = 21, pip: str = "P1", due: str | None = None):
    """Insert a production prediction.

    ``due`` overrides the written due date: the session-exact value the fixed
    collector writes, or a LEGACY calendar approximation (which precedes the
    session-exact exit -- the live rows' shape).
    """
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
                AS_OF,
                "production",
                0.5,
                "CA",
                1,
                1,
                "h",
                "c",
                "[]",
                horizon,
                due or _outcome_due_date(AS_OF, horizon),
                f"{AS_OF}T20:15:00Z",
                "production",
            ),
        )
        conn.commit()


def _run(exp, research_db, paths, collection: str) -> dict:
    return mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        collection_date=date.fromisoformat(collection),
    )


def _rows(exp: ExperimentDB) -> list[tuple]:
    with exp.connect(read_only=True) as conn:
        return conn.execute(
            "SELECT prediction_id, outcome_status, total_return, entry_session_date,"
            "       exit_session_date FROM forward_outcome"
        ).fetchall()


# 1 -----------------------------------------------------------------------
def test_horizon_not_yet_elapsed_is_awaiting_horizon_with_no_row(tmp_path):
    """The live shape: the advisory due date opens the gate before the 21st
    session has completed -> pending, no row."""
    sessions = _sessions_from(EXPECTED_ENTRY, 20)  # entry + 19 realized sessions
    paths, research_db, exp = _seed(tmp_path)
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    # legacy calendar due date (as_of + 30 days), exactly the live ledger's shape
    legacy_due = (date.fromisoformat(AS_OF) + timedelta(days=30)).isoformat()
    _prediction(exp, due=legacy_due)
    summary = _run(exp, research_db, paths, legacy_due)
    assert _rows(exp) == []
    assert summary["awaiting"]["AWAITING_HORIZON"] == 1
    assert summary["matured"] == {}


# 2 -----------------------------------------------------------------------
def test_horizon_elapsed_with_missing_exit_bar_is_awaiting_exit_bar(tmp_path):
    """One session short of the horizon, long after it elapsed: no permanent row."""
    sessions = _sessions_from(EXPECTED_ENTRY, 21)  # entry + 20 realized sessions
    paths, research_db, exp = _seed(tmp_path)
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    _prediction(exp)
    for collection in (EXIT_SESSION, "2027-01-04", "2028-01-03"):
        summary = _run(exp, research_db, paths, collection)
        assert _rows(exp) == [], f"a data gap must never become permanent: {summary}"
        assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1, summary
    # ...and it is exposed in health with age.
    health = forward_health(
        experiment_db=exp, paths=paths, collection_date=date.fromisoformat("2027-01-04")
    )
    block = health["awaiting_exit"]
    assert block["total"] == 1 and block["distinct_securities"] == 1
    assert block["oldest_as_of"] == AS_OF
    assert block["recoverable"]["securities"] == 1
    assert block["age_sessions_past_exit"] is not None


# 3 -----------------------------------------------------------------------
def test_a_backfilled_exit_bar_heals_the_gap_into_observed(tmp_path):
    """THE point of the policy: a temporary gap must still become OBSERVED."""
    sessions = _sessions_from(EXPECTED_ENTRY, 21)
    missing = sessions[21] if len(sessions) > 21 else required_exit_session(EXPECTED_ENTRY, 21)
    paths, research_db, exp = _seed(tmp_path)
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    _prediction(exp)

    # horizon elapsed, the required exit bar absent -> pending, no row
    summary = _run(exp, research_db, paths, missing)
    assert _rows(exp) == []
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1

    # backfill exactly the required exit session bar
    _insert_bar(research_db, missing, 200.0, tag="backfill")
    summary = _run(exp, research_db, paths, missing)
    rows = _rows(exp)
    assert len(rows) == 1, summary
    assert rows[0][1] == "OBSERVED"
    assert rows[0][4] == missing
    assert rows[0][2] == 200.0 / 100.0 - 1.0


# 4 -----------------------------------------------------------------------
def test_conflicting_exit_bars_keep_it_pending_not_censored(tmp_path):
    """A UNKNOWN session (conflicting duplicates) is a gap too, not a verdict."""
    sessions = _sessions_from(EXPECTED_ENTRY, 23)
    paths, research_db, exp = _seed(tmp_path)
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    exit_session = sessions[21]
    # two conflicting bars for the required exit session -> that session UNKNOWN
    _insert_bar(research_db, exit_session, 111.0, tag="conflict-a")
    _insert_bar(research_db, exit_session, 222.0, tag="conflict-b")
    _prediction(exp)

    for collection in (exit_session, "2027-06-01"):
        summary = _run(exp, research_db, paths, collection)
        assert _rows(exp) == [], f"never a permanent label: {summary}"
        assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1, summary


# 5 -----------------------------------------------------------------------
def test_a_verified_delisting_within_the_horizon_is_terminal(tmp_path):
    """Positive evidence under an existing explicit class -> terminal label."""
    sessions = _sessions_from(EXPECTED_ENTRY, 5)
    paths, research_db, exp = _seed(tmp_path, delisted_at=sessions[-1])
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    _prediction(exp)
    _run(exp, research_db, paths, "2027-06-01")
    rows = _rows(exp)
    assert len(rows) == 1
    assert rows[0][1] == "DELISTING_OUTCOME_UNKNOWN"
    assert rows[0][2] is None, "never an imputed return"


# 6 -----------------------------------------------------------------------
def test_repeated_pending_runs_are_idempotent_and_write_nothing(tmp_path):
    sessions = _sessions_from(EXPECTED_ENTRY, 20)
    paths, research_db, exp = _seed(tmp_path)
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    _prediction(exp)
    for _ in range(4):
        _run(exp, research_db, paths, EXIT_SESSION)
    assert _rows(exp) == []
    with exp.connect(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM forward_outcome").fetchone()[0] == 0


# 7 -----------------------------------------------------------------------
def test_frozen_snapshot_builder_keeps_its_terminal_censored_contract(tmp_path):
    """The research builder's contract is explicitly unchanged.

    A frozen dataset snapshot has a FINAL observation boundary, so an insufficient
    horizon is terminally CENSORED there -- unlike the live ledger above.
    """
    from tradehub_research.validation.outcome_builder import build_outcome_label

    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    exp = ExperimentDB(tmp_path / "experiment.db", 5000)
    exp.migrate()
    with exp.connect() as conn:
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
                TICKER,
                "US",
                "Test Co",
                "Technology",
                "HW",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
    # only 10 realized sessions against a 63-session horizon, inside a frozen snapshot
    for day in _sessions_from(EXPECTED_ENTRY, 10):
        _insert_bar(research_db, day, 100.0)

    label = build_outcome_label(
        research_db,
        exp,
        dataset_snapshot_id="snap-1",
        security_id=SECURITY,
        observation_date=f"{AS_OF}T20:15:00Z",
        horizon_sessions=63,
    )
    assert label["outcome_status"] == "CENSORED_INSUFFICIENT_HORIZON"
    assert label["total_return"] is None
    assert label["entry_session_date"] == EXPECTED_ENTRY


def test_the_live_path_never_emits_the_frozen_censored_label(tmp_path):
    """Explicit contrast: the same shortfall on the live ledger stays pending."""
    sessions = _sessions_from(EXPECTED_ENTRY, 10)
    paths, research_db, exp = _seed(tmp_path)
    _seed_bars(research_db, [(d, 100.0 + i) for i, d in enumerate(sessions)])
    _prediction(exp, horizon=63)
    for _ in range(3):
        summary = _run(exp, research_db, paths, "2027-06-01")
        assert summary["matured"] == {}, summary
    assert _rows(exp) == []
    assert last_completed_us_session() >= date(2026, 9, 1)  # live clock sanity
    assert entry_session_for(AS_OF) == EXPECTED_ENTRY

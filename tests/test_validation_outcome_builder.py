from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.outcome_builder import (
    build_outcome_label,
)


def _seed_security(db, security_id="sec-1", delisted_at=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                security_id,
                "TST",
                "NASDAQ",
                "Test Inc.",
                "Technology",
                "Hardware",
                "SUPPORTED",
                "2020-01-01T00:00:00Z",
                delisted_at,
            ),
        )


def _seed_price_bars(db, security_id, sessions):
    """sessions: list of (date, open, close). PAT = same date 20:15 UTC."""
    store = EvidenceStore(db)
    for date, open_, close in sessions:
        store.insert(
            security_id=security_id,
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": "TST",
                "session_date": date,
                "open": open_,
                "high": open_,
                "low": close,
                "close": close,
                "volume": 1000000,
            },
            extraction_confidence=1.0,
            event_time=f"{date}T20:15:00Z",
            public_available_time=f"{date}T20:15:00Z",
            pat_provenance="derived_from_index",
            source_record_id=f"rec-{date}",
        )


def _seed_split(db, security_id, effective_date, factor):
    EvidenceStore(db).insert(
        security_id=security_id,
        source_id="tiingo_eod",
        structured_fields={
            "record_type": "split",
            "provider_ticker": "TST",
            "effective_date": effective_date,
            "factor": factor,
        },
        extraction_confidence=1.0,
        event_time=f"{effective_date}T20:15:00Z",
        public_available_time=f"{effective_date}T20:15:00Z",
        pat_provenance="derived_from_index",
        source_record_id=f"rec-split-{effective_date}",
    )


def _setup(tmp_path, sessions, delisted_at=None):
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot VALUES "
            "('snap-1','abc',11,NULL,'{}','h1','/tmp/x','h2','{}','READY','2025-01-01T00:00:00Z')"
        )
    _seed_security(research_db, delisted_at=delisted_at)
    _seed_price_bars(research_db, "sec-1", sessions)
    return research_db, experiment_db


def test_full_outcome_uses_next_session_entry_and_63_session_exit(tmp_path):
    from datetime import date, timedelta

    sessions = []
    d = date(2024, 1, 1)
    for day in range(1, 100):
        sessions.append((d.isoformat(), 100.0, 100.0 + day))
        d += timedelta(days=1)
    research_db, experiment_db = _setup(tmp_path, sessions)

    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-01-01T00:00:00Z",
        horizon_sessions=63,
    )

    assert label["outcome_status"] == "OBSERVED"
    assert label["entry_convention"] == "next_session_open"
    assert label["entry_session_date"] == "2024-01-02"
    assert label["total_return"] is not None and label["total_return"] > 0


def test_delisted_name_never_disappears(tmp_path):
    """A security that delists before the exit horizon must be recorded as
    DELISTING_OUTCOME_UNKNOWN -- never dropped, never imputed zero."""
    sessions = [(f"2024-01-{day:02d}", 100.0, 100.0) for day in range(1, 32)]
    research_db, experiment_db = _setup(tmp_path, sessions, delisted_at="2024-01-15")

    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-01-02T00:00:00Z",
        horizon_sessions=252,
    )

    assert label["outcome_status"] == "DELISTING_OUTCOME_UNKNOWN"
    assert label["delisting_event_ref"] is not None


def test_insufficient_horizon_is_censored_not_dropped(tmp_path):
    """Fewer sessions than the horizon: CENSORED_INSUFFICIENT_HORIZON with a
    row present (never silently absent)."""
    sessions = [(f"2024-01-{day:02d}", 100.0, 101.0) for day in range(1, 11)]
    research_db, experiment_db = _setup(tmp_path, sessions)

    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-01-01T00:00:00Z",
        horizon_sessions=63,
    )

    assert label["outcome_status"] == "CENSORED_INSUFFICIENT_HORIZON"
    assert label["total_return"] is None


def test_entry_unavailable_when_no_future_session(tmp_path):
    sessions = [("2024-01-01", 100.0, 100.0)]
    research_db, experiment_db = _setup(tmp_path, sessions)

    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-12-31T00:00:00Z",
        horizon_sessions=21,
    )

    assert label["outcome_status"] == "ENTRY_UNAVAILABLE"


def test_split_adjusts_total_return_but_not_raw(tmp_path):
    """A 2:1 split inside the window: raw return uses raw closes; total
    return applies the cumulative factor."""
    sessions = [(f"2024-01-{day:02d}", 100.0, 100.0) for day in range(1, 31)]
    research_db, experiment_db = _setup(tmp_path, sessions)
    _seed_split(research_db, "sec-1", "2024-01-10", 2.0)

    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-01-02T00:00:00Z",
        horizon_sessions=21,
    )

    assert label["outcome_status"] == "OBSERVED"
    assert label["raw_return"] is not None
    assert label["total_return"] is not None
    # After a 2:1 split the pre-split price history is halved in adjusted
    # terms; total return must reflect the factor while raw return does not.
    assert label["total_return"] != label["raw_return"]


def test_benchmark_relative_return(tmp_path):
    sessions = [(f"2024-01-{day:02d}", 100.0, 100.0 + day) for day in range(1, 31)]
    research_db, experiment_db = _setup(tmp_path, sessions)

    benchmark_returns = {f"2024-01-{day:02d}": 0.001 for day in range(1, 31)}
    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-01-02T00:00:00Z",
        horizon_sessions=21,
        benchmark_id="bench-1",
        benchmark_daily_returns=benchmark_returns,
    )

    assert label["benchmark_id"] == "bench-1"
    assert label["benchmark_return"] is not None
    assert label["benchmark_relative_return"] is not None


def test_repeated_build_is_idempotent(tmp_path):
    sessions = [(f"2024-01-{day:02d}", 100.0, 100.0 + day) for day in range(1, 31)]
    research_db, experiment_db = _setup(tmp_path, sessions)

    for _ in range(2):
        build_outcome_label(
            research_db,
            experiment_db,
            dataset_snapshot_id="snap-1",
            security_id="sec-1",
            observation_date="2024-01-02T00:00:00Z",
            horizon_sessions=21,
        )

    with experiment_db.connect(read_only=True) as conn:
        count = conn.execute("SELECT COUNT(*) FROM outcome_label").fetchone()[0]
    assert count == 1  # identical (security, observation, horizon, snapshot) dedupes

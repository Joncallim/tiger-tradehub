from tradehub_research.db import ResearchDB
from tradehub_research.universe import UniverseMembershipStore
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.pit_grid import (
    event_time_grid,
    grid_mode_label,
    monthly_pit_grid,
)
from tradehub_research.validation.replay import (
    load_screen_results,
    replay_monthly_grid,
    screen_observation_date,
)
from tradehub_research.validation.snapshot_builder import build_validation_snapshot


def test_monthly_pit_grid_uses_session_end_timestamps():
    grid = monthly_pit_grid("2024-01-01T00:00:00Z", "2024-03-31T00:00:00Z")
    assert len(grid) == 3
    # Jan 31 20:15 America/New_York (EST, UTC-5) = Feb 1 01:15 UTC
    assert grid[0] == "2024-02-01T01:15:00Z"
    # Feb 29 20:15 EST = Mar 1 01:15 UTC (2024 is a leap year)
    assert grid[1] == "2024-03-01T01:15:00Z"
    # Mar 31 20:15 America/New_York (EDT, UTC-4 -- DST) = Apr 1 00:15 UTC
    assert grid[2] == "2024-04-01T00:15:00Z"


def test_event_time_grid_labels_mode():
    monthly = monthly_pit_grid("2024-01-01", "2024-02-01")
    event = event_time_grid(["2024-01-05T10:00:00Z", "2024-01-07T09:30:00Z"])
    assert len(event) == 2
    assert grid_mode_label(event[0], monthly) == "event_time"
    assert grid_mode_label(monthly[0], monthly) == "monthly"


def _seed_replayable_db(tmp_path, as_of="2025-04-01T00:00:00Z"):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("S", "S", "NYSE", "S", None, None, "SUPPORTED", "2020-01-01", None),
        )
    UniverseMembershipStore(database).insert(
        security_id="S",
        price=10,
        market_cap=1e9,
        avg_dollar_volume=1e7,
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
        valid_from="2020-01-01",
        knowledge_time="2025-01-01",
        pat_provenance="derived_from_index",
    )
    return database


def test_replay_production_path_is_deterministic_and_date_keyed(tmp_path):
    """The replay path must (a) run the actual production run_screening,
    (b) be deterministic, and (c) key observations on pipeline_run.as_of --
    NEVER on computed_at (the run's wall clock)."""
    research_db = _seed_replayable_db(tmp_path)
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    dataset_snapshot_id = build_validation_snapshot(
        research_db, experiment_db, dest_dir=tmp_path / "snapshots"
    )

    replay_db = ResearchDB(tmp_path / "replay.db")
    replay_db.migrate()

    grid = ["2025-04-01T00:00:00Z", "2025-05-01T00:00:00Z"]
    run_ids = replay_monthly_grid(
        experiment_db,
        replay_db,
        dataset_snapshot_id=dataset_snapshot_id,
        grid_timestamps=grid,
    )
    assert set(run_ids) == set(grid)

    # Deterministic: replaying the same grid against the same snapshot
    # reproduces identical run_ids (the production pipeline is
    # insert-or-verify; differing stored hashes would fail the run).
    replay_db2 = ResearchDB(tmp_path / "replay2.db")
    replay_db2.migrate()
    run_ids2 = replay_monthly_grid(
        experiment_db,
        replay_db2,
        dataset_snapshot_id=dataset_snapshot_id,
        grid_timestamps=grid,
    )
    assert run_ids == run_ids2

    # Date-keying: every loaded screen row's observation date is the GRID
    # date (as_of), not the replay run's wall-clock date.
    rows = load_screen_results(replay_db)
    assert rows
    for row in rows:
        assert screen_observation_date(row) == row["as_of"][:10]
        assert screen_observation_date(row) in {"2025-04-01", "2025-05-01"}
        # computed_at is the run's wall clock, NOT the evaluation date.
        assert screen_observation_date(row) != row["computed_at"][:10]
    assert any(screen_observation_date(r) == "2025-04-01" for r in rows)
    assert any(screen_observation_date(r) == "2025-05-01" for r in rows)


def test_replay_runs_through_snapshot_not_live_db(tmp_path):
    """Replay reads the frozen snapshot; mutating the live DB afterwards
    must not change replayed results."""
    research_db = _seed_replayable_db(tmp_path)
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    dataset_snapshot_id = build_validation_snapshot(
        research_db, experiment_db, dest_dir=tmp_path / "snapshots"
    )
    replay_db = ResearchDB(tmp_path / "replay.db")
    replay_db.migrate()
    run_ids = replay_monthly_grid(
        experiment_db,
        replay_db,
        dataset_snapshot_id=dataset_snapshot_id,
        grid_timestamps=["2025-04-01T00:00:00Z"],
    )

    # Mutate the live DB: add a second universe member AFTER the snapshot.
    with research_db.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("S2", "S2", "NYSE", "S2", None, None, "SUPPORTED", "2020-01-01", None),
        )
    UniverseMembershipStore(research_db).insert(
        security_id="S2",
        price=10,
        market_cap=1e9,
        avg_dollar_volume=1e7,
        price_eligible=True,
        market_cap_eligible=True,
        liquidity_eligible=True,
        eligible=True,
        valid_from="2020-01-01",
        knowledge_time="2025-01-01",
        pat_provenance="derived_from_index",
    )

    replay_db2 = ResearchDB(tmp_path / "replay2.db")
    replay_db2.migrate()
    run_ids2 = replay_monthly_grid(
        experiment_db,
        replay_db2,
        dataset_snapshot_id=dataset_snapshot_id,
        grid_timestamps=["2025-04-01T00:00:00Z"],
    )
    assert run_ids == run_ids2  # frozen snapshot immune to live-DB mutation

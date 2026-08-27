import pytest

from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.regime import (
    InsufficientCoverageError,
    draft_evaluation_regime,
    load_evaluation_regime,
    seal_evaluation_regime,
)


def _seeded_db(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot VALUES "
            "('s1','abc',11,NULL,'{}','h1','/tmp/x','h2','{}','READY','2025-01-01T00:00:00Z')"
        )
    return experiment_db


def test_short_coverage_window_raises_insufficient_data(tmp_path):
    db = _seeded_db(tmp_path)
    with pytest.raises(InsufficientCoverageError):
        draft_evaluation_regime(db, "s1", coverage_start="2024-01-01", coverage_end="2024-06-01")


def test_regime_never_shrinks_minimums_to_force_a_result(tmp_path):
    """A coverage window that is long enough for the max horizon but too
    short to carve BOTH a minimum development window AND a minimum holdout
    must still raise -- never silently produce an undersized regime."""
    db = _seeded_db(tmp_path)
    with pytest.raises(InsufficientCoverageError):
        draft_evaluation_regime(db, "s1", coverage_start="2023-01-01", coverage_end="2024-06-01")


def test_reasonable_window_drafts_unsealed_regime(tmp_path):
    db = _seeded_db(tmp_path)
    regime_id = draft_evaluation_regime(
        db, "s1", coverage_start="2018-01-01", coverage_end="2025-01-01"
    )
    regime = load_evaluation_regime(db, regime_id)
    assert regime["sealed_at"] is None
    spec = regime["spec"]
    assert spec["development_window"]["start"] == "2018-01-01"
    assert spec["holdout_window"]["end"] == spec["label_maturity_cutoff"]


def test_seal_is_one_time_and_dates_never_change(tmp_path):
    import sqlite3

    db = _seeded_db(tmp_path)
    regime_id = draft_evaluation_regime(
        db, "s1", coverage_start="2018-01-01", coverage_end="2025-01-01"
    )
    before = load_evaluation_regime(db, regime_id)

    seal_evaluation_regime(db, regime_id)
    after = load_evaluation_regime(db, regime_id)
    assert after["sealed_at"] is not None
    assert after["spec_json"] == before["spec_json"]

    with pytest.raises(ValueError, match="already sealed"):
        seal_evaluation_regime(db, regime_id)

    # Attempting to alter the spec post-seal must hit the DB trigger too.
    with pytest.raises(sqlite3.IntegrityError, match="only permits one seal transition"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE evaluation_regime SET spec_json='{}' WHERE regime_id=?", (regime_id,)
            )


def test_sealed_regime_blocks_non_holdout_attempts(tmp_path):
    import sqlite3

    db = _seeded_db(tmp_path)
    regime_id = draft_evaluation_regime(
        db, "s1", coverage_start="2018-01-01", coverage_end="2025-01-01"
    )
    seal_evaluation_regime(db, regime_id)

    with pytest.raises(sqlite3.IntegrityError, match="regime sealed"):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO experiment_attempt VALUES "
                "('a1',?,'s1','BASELINE','B0','{}','h4',NULL,NULL,1,'COMPLETE',NULL,"
                "'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')",
                (regime_id,),
            )

    # A HOLDOUT attempt must still be allowed post-seal.
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO experiment_attempt VALUES "
            "('a2',?,'s1','HOLDOUT','FINAL','{}','h5',NULL,NULL,1,'COMPLETE',NULL,"
            "'2025-01-01T00:00:00Z','2025-01-01T00:00:00Z')",
            (regime_id,),
        )

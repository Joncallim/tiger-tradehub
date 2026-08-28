import hashlib

from tradehub_research.validation.benchmark import (
    load_benchmark_daily_returns,
    parse_ff_daily_factors,
    pin_benchmark_artifact,
)
from tradehub_research.validation.experiment_db import ExperimentDB

FF_FIXTURE = """\
Mkt-RF  SMB  HML  RF
19260701  0.29  -0.05  -0.21  0.009
19260702  0.05  0.01  0.09  0.009
19260706  0.41  -0.03  0.21  0.009
19260707  0.44  -0.06  0.29  0.009
19260708  -0.04  -0.09  0.14  0.009
19260709  -0.30  -0.16  0.10  0.009
Annual Factors: January-December
1926   0.34  0.95  -0.01  0.01
"""


def test_parse_ff_daily_factors(tmp_path):
    rows, series_hash = parse_ff_daily_factors(FF_FIXTURE)

    assert len(rows) == 6  # annual-summary tail and header skipped
    # 19260701: Mkt-RF 0.29% + RF 0.009% -> 0.299% -> 0.00299 decimal
    assert abs(rows["1926-07-01"] - 0.00299) < 1e-9
    assert abs(rows["1926-07-09"] - (-0.00291)) < 1e-9
    # Keys are normalized to ISO YYYY-MM-DD so the outcome builder's
    # session-date comparisons (ISO) can match the series.
    assert "19260701" not in rows
    assert len(series_hash) == 64


def test_pin_and_load_benchmark_artifact(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    cache_path = tmp_path / "benchmark-cache" / "ff-daily"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(FF_FIXTURE, encoding="utf-8")
    raw_hash = hashlib.sha256(FF_FIXTURE.encode()).hexdigest()
    _, parsed_hash = parse_ff_daily_factors(FF_FIXTURE)

    benchmark_id = pin_benchmark_artifact(
        experiment_db,
        source="kenneth_french",
        source_url="https://mba.tuck.dartmouth.edu/...",
        vintage_label="2026-08-v1",
        raw_content_hash=raw_hash,
        parsed_series_hash=parsed_hash,
        cache_path=str(cache_path),
    )

    loaded = load_benchmark_daily_returns(experiment_db, benchmark_id)
    assert loaded["1926-07-01"] == 0.00299


def test_tampered_benchmark_fails_closed(tmp_path):
    import pytest

    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    cache_path = tmp_path / "ff-daily"
    cache_path.write_text(FF_FIXTURE, encoding="utf-8")
    raw_hash = hashlib.sha256(FF_FIXTURE.encode()).hexdigest()
    _, parsed_hash = parse_ff_daily_factors(FF_FIXTURE)
    benchmark_id = pin_benchmark_artifact(
        experiment_db,
        source="kenneth_french",
        source_url="url",
        vintage_label="v1",
        raw_content_hash=raw_hash,
        parsed_series_hash=parsed_hash,
        cache_path=str(cache_path),
    )

    # Rotate the cache file: must fail closed, never silently swap series.
    cache_path.write_text(FF_FIXTURE.replace("0.29", "0.99"), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash mismatch"):
        load_benchmark_daily_returns(experiment_db, benchmark_id)

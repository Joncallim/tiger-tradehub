"""End-to-end pipeline test: real snapshot -> replay -> outcomes -> baselines
-> ablations -> walk-forward -> sealed holdout, with a non-empty fixture
universe so the machinery is exercised with real observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta

from tradehub_research.db import ResearchDB
from tradehub_research.validation.benchmark import (
    parse_ff_daily_factors,
    pin_benchmark_artifact,
)
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.pipeline import run_pipeline
from tradehub_research.validation.regime import draft_evaluation_regime
from tradehub_research.validation.snapshot_builder import build_validation_snapshot

FF_FIXTURE = """\
 Mkt-RF   SMB   HML    RF
20210104    0.10 -0.01  0.02  0.01
20210201    0.05  0.00  0.01  0.01
20210301   -0.03  0.02 -0.01  0.01
20210401    0.08 -0.02  0.00  0.01
20210503    0.02  0.01  0.01  0.01
20210601    0.06  0.00 -0.02  0.01
20210701   -0.04  0.01  0.01  0.01
20210802    0.03  0.00  0.00  0.01
20210901    0.07 -0.01  0.01  0.01
20211001   -0.02  0.02 -0.01  0.01
20211101    0.05  0.00  0.01  0.01
20211201    0.04 -0.01  0.00  0.01
20220103    0.06  0.01 -0.01  0.01
20220201   -0.05  0.00  0.01  0.01
20220301    0.09 -0.02  0.00  0.01
20220401    0.01  0.01  0.01  0.01
20220502    0.04  0.00 -0.01  0.01
20220601    0.02  0.00  0.01  0.01
"""


def _weekdays(start: date, end: date):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


def _seed_research_db(tmp_path, membership_at: str = "2020-01-01T00:00:00Z") -> ResearchDB:
    from tradehub_research.evidence import EvidenceStore

    db = ResearchDB(tmp_path / "research.db")
    db.migrate()
    store = EvidenceStore(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("sec_xbrl", "regulatory_filing", 1, "test", "derived_from_index"),
        )
        for i, sid in enumerate(("S1", "S2", "S3", "S4")):
            conn.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    f"T{i}",
                    "US",
                    "Co",
                    "Technology",
                    "HW",
                    "SUPPORTED",
                    "2020-01-01T00:00:00Z",
                    None,
                ),
            )
            conn.execute(
                "INSERT INTO universe_membership "
                "(security_id,price,market_cap,avg_dollar_volume,price_eligible,"
                "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,"
                "knowledge_time,pat_provenance,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    None,
                    None,
                    None,
                    0,
                    0,
                    0,
                    1,
                    membership_at,
                    None,
                    membership_at,
                    "derived_from_index",
                    None,
                ),
            )

    # Weekly price bars 2020-01-01 -> 2022-06-30 with PIT PAT (20:15 ET).
    bars = []
    for sid, drift in (("S1", 0.001), ("S2", -0.0005), ("S3", 0.0003), ("S4", -0.0002)):
        price = 50.0
        for day in _weekdays(date(2020, 1, 1), date(2022, 6, 30)):
            price = max(5.0, price * (1 + drift))
            bars.append(
                {
                    "security_id": sid,
                    "session_date": day.isoformat(),
                    "close": round(price, 2),
                    "open": round(price * 0.99, 2),
                    "high": round(price * 1.01, 2),
                    "low": round(price * 0.98, 2),
                    "volume": 1_000_000,
                }
            )
    for bar in bars:
        store.insert(
            security_id=bar["security_id"],
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": bar["security_id"],
                "session_date": bar["session_date"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
            },
            extraction_confidence=1.0,
            event_time=f"{bar['session_date']}T00:00:00Z",
            public_available_time=f"{bar['session_date']}T20:15:00Z",
            pat_provenance="derived_from_index",
            source_record_id=f"{bar['security_id']}:{bar['session_date']}:bar",
        )

    # Annual XBRL facts (365-day durations) + shares instants, 2020-2021.
    # S4 intentionally has NO facts: its fundamental families are
    # insufficient (confidence 0), giving B4/ablation signals real
    # cross-sectional variance (data availability differs by security).
    facts = []
    for sid, margin in (("S1", 0.2), ("S2", -0.05), ("S3", 0.12)):
        for year in (2020, 2021):
            filed = f"{year + 1}-02-28"
            revenue = 100_000_000 + (10_000_000 if sid == "S1" else 0)
            concepts = [
                ("RevenueFromContractWithCustomerExcludingAssessedTax", revenue),
                ("NetIncomeLoss", revenue * margin),
                ("NetCashProvidedByUsedInOperatingActivities", revenue * margin * 0.9),
                ("PaymentsToAcquirePropertyPlantAndEquipment", revenue * 0.05),
                ("Assets", revenue * 1.5),
            ]
            for concept, value in concepts:
                facts.append(
                    {
                        "security_id": sid,
                        "concept": concept,
                        "start": f"{year}-01-01",
                        "end": f"{year}-12-31",
                        "value": value,
                        "filed": filed,
                        "accn": f"{sid}-{year}-10k",
                    }
                )
            facts.append(
                {
                    "security_id": sid,
                    "concept": "EntityCommonStockSharesOutstanding",
                    "start": None,
                    "end": f"{year}-12-31",
                    "value": 10_000_000,
                    "filed": filed,
                    "accn": f"{sid}-{year}-10k",
                }
            )
    for fact in facts:
        pat = f"{fact['filed']}T00:00:00Z"
        store.insert(
            security_id=fact["security_id"],
            source_id="sec_xbrl",
            structured_fields={
                "record_type": "xbrl_fact",
                "metric": fact["concept"],
                "tag": fact["concept"],
                "concept": fact["concept"],
                "unit": "USD",
                "value": fact["value"],
                "start": fact["start"],
                "end": fact["end"],
                "period_start": fact["start"],
                "period_end": fact["end"],
                "accession": fact["accn"],
                "filed": fact["filed"],
            },
            extraction_confidence=1.0,
            event_time=fact["end"] or fact["filed"],
            public_available_time=pat,
            pat_provenance="derived_from_index",
            source_record_id=f"{fact['security_id']}:{fact['accn']}:{fact['concept']}",
        )
    return db


def _seed_experiment_db(tmp_path, research_db) -> tuple[ExperimentDB, str, str, str]:
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    sample = [{"cik": f"S{i}", "ticker": f"T{i - 1}", "title": f"Co {i}"} for i in range(1, 5)]
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO universe_sample VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "sample-1",
                "url",
                "h" * 64,
                1,
                "sha256(seed+NUL+ticker) ascending take-450; BOOTSTRAP_COHORT",
                2,
                json.dumps(sample),
                2,
                "2026-08-27T00:00:00Z",
            ),
        )
    snapshot_id = build_validation_snapshot(
        research_db,
        experiment_db,
        dest_dir=tmp_path / "snapshots",
        scope="test snapshot",
        universe_sample_id="sample-1",
    )
    # Pin a benchmark artifact from the fixture (no network).
    cache = tmp_path / "cache" / "benchmark"
    cache.mkdir(parents=True, exist_ok=True)
    cache_path = cache / "ff.txt"
    cache_path.write_text(FF_FIXTURE, encoding="utf-8")
    raw_hash = hashlib.sha256(FF_FIXTURE.encode()).hexdigest()
    series, parsed_hash = parse_ff_daily_factors(FF_FIXTURE)
    benchmark_id = pin_benchmark_artifact(
        experiment_db,
        source="ken-french-daily-factors",
        source_url="https://example.invalid/ff",
        vintage_label="test",
        raw_content_hash=raw_hash,
        parsed_series_hash=parsed_hash,
        cache_path=str(cache_path),
    )
    regime_id = draft_evaluation_regime(
        experiment_db, snapshot_id, coverage_start="2021-01-01", coverage_end="2022-06-30"
    )
    return experiment_db, snapshot_id, regime_id, benchmark_id


def test_pipeline_full_sequence(tmp_path):
    research_db = _seed_research_db(tmp_path)
    experiment_db, snapshot_id, regime_id, benchmark_id = _seed_experiment_db(tmp_path, research_db)

    result = run_pipeline(
        experiment_db,
        research_db,
        dataset_snapshot_id=snapshot_id,
        regime_id=regime_id,
        benchmark_id=benchmark_id,
        replay_db_path=tmp_path / "replay.db",
    )

    assert result["cohort"]["cohort"] == "BOOTSTRAP_COHORT"
    assert result["grid"]["count"] >= 15
    assert result["replay"]["screen_results"] >= 15 * 4 * 6  # grid x securities x families
    assert result["replay"]["screenable_observations"] >= 15 * 4
    assert result["outcomes"]["labels"] >= 15 * 4 * 4  # obs x horizons
    statuses = set(result["outcomes"]["by_status"])
    assert "OBSERVED" in statuses  # early observations have matured labels

    # Baselines ran with real observations (not vacuous).
    for baseline in (
        "B0_BENCHMARK",
        "B1_UNIVERSE",
        "B2_FACTOR_COMPOSITE",
        "B3_HUNTERS_ONLY",
        "B4_EQUAL_SCORING",
    ):
        summary = result["baselines"][baseline]
        h = summary["horizons"]["21"]
        assert h.get("date_count", 0) >= 3, (baseline, h)
    assert result["holdout"]["B4_EQUAL_SCORING"]["status"] == "COMPLETE"
    assert result["attempt_ledger"]["variants"] >= 20

    # The regime is sealed now: further non-HOLDOUT attempts are blocked.
    import pytest

    with pytest.raises(sqlite3.Error):
        from tradehub_research.validation.attempt_ledger import start_attempt

        start_attempt(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id=snapshot_id,
            variant_kind="BASELINE",
            variant_name="B4_EQUAL_SCORING",
            config={"baseline": "B4_EQUAL_SCORING"},
        )
    # The sealed holdout is one-time: a second holdout run refuses.
    from tradehub_research.validation.holdout import run_sealed_holdout
    from tradehub_research.validation.replay import load_screen_results

    replay_db = ResearchDB(tmp_path / "replay.db")
    screens = load_screen_results(replay_db)
    with experiment_db.connect(read_only=True) as conn:
        labels = conn.execute("SELECT * FROM outcome_label").fetchall()
    with pytest.raises(ValueError, match="one-time"):
        run_sealed_holdout(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id=snapshot_id,
            baseline="B4_EQUAL_SCORING",
            screens=screens,
            outcome_labels=[dict(row) for row in labels],
        )


def test_pipeline_vacuous_insufficient_when_no_screens(tmp_path):
    """With a universe that never enters the grid window, every evaluation
    records honest INSUFFICIENT_DATA -- never a fabricated pass."""
    research_db = _seed_research_db(tmp_path, membership_at="2023-01-01T00:00:00Z")
    experiment_db, snapshot_id, regime_id, benchmark_id = _seed_experiment_db(tmp_path, research_db)

    result = run_pipeline(
        experiment_db,
        research_db,
        dataset_snapshot_id=snapshot_id,
        regime_id=regime_id,
        benchmark_id=benchmark_id,
        replay_db_path=tmp_path / "replay2.db",
    )

    assert result["replay"]["screen_results"] == 0
    assert result["outcomes"]["labels"] == 0
    assert result["holdout"]["B4_EQUAL_SCORING"]["status"] == "INSUFFICIENT_DATA"
    assert result["attempt_ledger"]["variants"] >= 20  # every variant recorded
    by_status = result["attempt_ledger"]["by_status"]
    assert "INSUFFICIENT_DATA" in by_status

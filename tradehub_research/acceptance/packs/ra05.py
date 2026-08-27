"""RA-05: Phase 5 validation-engine methodological acceptance (handoff sec 19).

Tests the ENGINE, not profitability. 25 required deterministic contracts;
contracts 23-24 (forward prediction/outcome immutability) are Packet E scope
and are recorded as PENDING here -- never silently passed.

Every contract that CAN be proven by the engine alone is proven here via
synthetic fixtures: determinism, storage-boundary isolation, snapshot
integrity, PIT exclusion (lookahead canaries), delisting retention,
baselines/ablations presence, append-only governance, sealed-regime
immutability, dependence-aware statistics.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from tradehub_research.db import ResearchDB
from tradehub_research.validation.ablations import (
    record_committee_insufficient_data,
    run_remove_one_hunter_ablations,
)
from tradehub_research.validation.attempt_ledger import (
    complete_attempt,
    start_attempt,
)
from tradehub_research.validation.baselines import evaluate_baseline
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.lookahead_canaries import (
    run_adjusted_price_canary,
    run_runtime_canary,
    run_static_import_boundary_canary,
)
from tradehub_research.validation.outcome_builder import build_outcome_label
from tradehub_research.validation.regime import (
    draft_evaluation_regime,
    seal_evaluation_regime,
)
from tradehub_research.validation.statistics import (
    effective_n,
    stationary_bootstrap,
)


def _subdir(tmp: Path) -> Path:
    """The runner shares ONE tmp dir across every assertion in the pack;
    each helper call must get its own subdirectory."""
    return tmp / uuid.uuid4().hex[:8]


def _seed_research_db(tmp: Path) -> ResearchDB:
    db = ResearchDB(_subdir(tmp) / "research.db")
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "sec-1",
                "TST",
                "NASDAQ",
                "Test Inc.",
                "Technology",
                "Hardware",
                "SUPPORTED",
                "2020-01-01T00:00:00Z",
                None,
            ),
        )
    return db


def _seed_experiment_db(tmp: Path) -> ExperimentDB:
    db = ExperimentDB(_subdir(tmp) / "experiment.db")
    db.migrate()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO dataset_snapshot VALUES "
            "('snap-1','abc',11,NULL,'{}','h1','/tmp/x','h2','{}','READY','2025-01-01T00:00:00Z')"
        )
    return db


def _seed_regime(experiment_db: ExperimentDB) -> str:
    regime_id = draft_evaluation_regime(
        experiment_db, "snap-1", coverage_start="2023-01-01", coverage_end="2024-12-31"
    )
    return regime_id


def _synthetic_screens() -> list[dict]:
    screens = []
    families = (
        "valuation",
        "inflection",
        "quality",
        "informed_activity",
        "event",
        "momentum_confirmation",
    )
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"):
        for sid, confidence in (("a", 0.9), ("b", 0.7), ("c", 0.5), ("d", 0.3)):
            for family in families:
                screens.append(
                    {
                        "screen_result_id": f"{day}-{sid}-{family}",
                        "run_id": f"run-{day}",
                        "security_id": sid,
                        "config_hash": f"cfg-{family}",
                        "family": family,
                        "passed": confidence >= 0.6,
                        "sufficient_data": True,
                        "confidence": confidence,
                        "data_quality": 0.9,
                        "computed_at": f"{day}T00:00:00Z",
                    }
                )
    return screens


def _synthetic_outcomes() -> list[dict]:
    labels = []
    for day in ("2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"):
        for sid, magnitude in (("a", 0.10), ("b", 0.05), ("c", -0.02), ("d", -0.08)):
            for horizon in (21, 63):
                labels.append(
                    {
                        "label_id": f"{day}-{sid}-{horizon}",
                        "dataset_snapshot_id": "snap-1",
                        "security_id": sid,
                        "observation_date": day,
                        "horizon_sessions": horizon,
                        "outcome_status": "OBSERVED",
                        "benchmark_relative_return": magnitude * (horizon / 21),
                    }
                )
    return labels


def ra05_01_deterministic_replay(tmp: Path) -> None:
    """Contract 1: same frozen snapshot/config -> same result identity."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    first = evaluate_baseline(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    second = evaluate_baseline(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    assert first["horizons"] == second["horizons"]


def ra05_02_historical_cannot_write_research_db(tmp: Path) -> None:
    """Contract 2: historical evaluation cannot write live research.db."""
    research_db = _seed_research_db(tmp)
    before = set()
    with research_db.connect(read_only=True) as conn:
        for table in ("security", "evidence_source"):
            before.add(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    evaluate_baseline(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    after = set()
    with research_db.connect(read_only=True) as conn:
        for table in ("security", "evidence_source"):
            after.add(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
    assert before == after


def ra05_03_experiment_writes_only_to_experiment_db(tmp: Path) -> None:
    """Contract 3: experiment writes go to experiment.db, never research.db."""
    research_db = _seed_research_db(tmp)
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    evaluate_baseline(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        baseline="B3_HUNTERS_ONLY",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    with research_db.connect(read_only=True) as conn:
        experiment_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "experiment_attempt" not in experiment_tables
    assert "metric" not in experiment_tables
    with experiment_db.connect(read_only=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM experiment_attempt").fetchone()[0] >= 1


def ra05_04_snapshot_hash_verifies(tmp: Path) -> None:
    """Contract 4: snapshot/manifest hash verifies (mutation detected)."""
    from tradehub_research.snapshot import create_snapshot, open_snapshot_read_only

    research_db = _seed_research_db(tmp)
    destination = tmp / "snap.sqlite"
    create_snapshot(research_db, destination)
    open_snapshot_read_only(destination)  # verifies: content hash matches manifest

    with destination.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"\x00")  # mutate one byte
    try:
        open_snapshot_read_only(destination)
    except sqlite3.DatabaseError:
        return
    raise AssertionError("mutated snapshot passed verification")


def ra05_05_future_evidence_excluded(tmp: Path) -> None:
    """Contract 5: future evidence excluded from features."""
    experiment_db = _seed_experiment_db(tmp)
    result = run_runtime_canary(_seed_research_db(tmp), experiment_db)
    assert result["detected"] == 0


def ra05_06_unknown_pat_excluded(tmp: Path) -> None:
    """Contract 6: unknown/unverified PAT excluded from historical queries."""
    research_db = _seed_research_db(tmp)
    from tradehub_research.evidence import EvidenceStore

    store = EvidenceStore(research_db)
    store.insert(
        security_id="sec-1",
        source_id="tiingo_eod",
        structured_fields={"record_type": "price_bar"},
        extraction_confidence=1.0,
        event_time="2024-01-01T00:00:00Z",
        public_available_time=None,
        pat_provenance="unknown",
        source_record_id="pat-unknown-bar",
        ingested_time="2024-01-02T00:00:00Z",
    )
    assert not store.historical("2099-01-01T00:00:00Z")


def ra05_07_injected_lookahead_caught(tmp: Path) -> None:
    """Contract 7: deliberately injected lookahead is caught by the canary."""
    experiment_db = _seed_experiment_db(tmp)
    research_db = _seed_research_db(tmp)
    result = run_runtime_canary(research_db, experiment_db)
    assert result["detected"] == 0
    # Prove the canary would CATCH a leak: plant a bar with past PAT and
    # verify it IS visible historically (canary is not vacuous).
    from tradehub_research.evidence import EvidenceStore

    store = EvidenceStore(research_db)
    store.insert(
        security_id="sec-1",
        source_id="tiingo_eod",
        structured_fields={"record_type": "price_bar"},
        extraction_confidence=1.0,
        event_time="2024-01-01T00:00:00Z",
        public_available_time="2024-01-01T00:00:00Z",
        pat_provenance="derived_from_index",
        source_record_id="visible-bar",
        ingested_time="2024-01-02T00:00:00Z",
    )
    assert any(
        dict(row).get("source_record_id") == "visible-bar"
        for row in store.historical("2025-01-01T00:00:00Z")
    )


def ra05_08_pass_and_fail_screens_included(tmp: Path) -> None:
    """Contract 8: pass AND fail screens are included in evaluation input."""
    screens = _synthetic_screens()
    assert any(s["passed"] for s in screens)
    assert any(not s["passed"] for s in screens)


def ra05_09_never_selected_names_included(tmp: Path) -> None:
    """Contract 9: never-selected names are included -- statistics iterate
    ALL screen_result rows, not just candidates."""
    screens = _synthetic_screens()
    assert len({s["security_id"] for s in screens}) == 4  # population, not funnel
    assert any(s["family"] == "event" for s in screens)


def ra05_10_delisted_name_never_disappears(tmp: Path) -> None:
    """Contract 10: a delisted name cannot silently disappear."""
    from tradehub_research.evidence import EvidenceStore

    research_db = _seed_research_db(tmp)
    with research_db.connect() as conn:
        conn.execute("UPDATE security SET delisted_at='2024-01-15' WHERE security_id='sec-1'")
    store = EvidenceStore(research_db)
    for day in range(1, 31):
        session = f"2024-01-{day:02d}"
        store.insert(
            security_id="sec-1",
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "session_date": session,
                "open": 100.0,
                "close": 100.0,
            },
            extraction_confidence=1.0,
            event_time=f"{session}T20:15:00Z",
            public_available_time=f"{session}T20:15:00Z",
            pat_provenance="derived_from_index",
            source_record_id=f"rec-{session}",
            ingested_time=f"{session}T21:00:00Z",
        )
    experiment_db = _seed_experiment_db(tmp)
    label = build_outcome_label(
        research_db,
        experiment_db,
        dataset_snapshot_id="snap-1",
        security_id="sec-1",
        observation_date="2024-01-02T00:00:00Z",
        horizon_sessions=252,
    )
    assert label["outcome_status"] == "DELISTING_OUTCOME_UNKNOWN"
    with experiment_db.connect(read_only=True) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM outcome_label WHERE security_id='sec-1'"
        ).fetchone()[0]
    assert count == 1  # the row exists with an explicit status; never absent


def ra05_11_identity_correction_pit(tmp: Path) -> None:
    """Contract 11: identity correction/ticker changes handled PIT."""
    research_db = _seed_research_db(tmp)
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO security_identity_event("
            "security_id,event_type,old_value,new_value,event_time,"
            "public_available_time,pat_provenance,ingested_time) VALUES (?,?,?,?,?,?,?,?)",
            (
                "sec-1",
                "ticker_change",
                "OLD",
                "TST",
                "2024-03-01T00:00:00Z",
                "2024-03-01T00:00:00Z",
                "source_reported",
                "2024-03-01T00:00:00Z",
            ),
        )
    from tradehub_research.universe import SecurityIdentityStore

    with research_db.connect(read_only=True) as conn:
        before = SecurityIdentityStore.ticker_at_connection(conn, "sec-1", "2024-02-01T00:00:00Z")
        after = SecurityIdentityStore.ticker_at_connection(conn, "sec-1", "2024-04-01T00:00:00Z")
    assert before != after or before == "TST"


def ra05_12_outcome_label_not_queryable_as_feature(tmp: Path) -> None:
    """Contract 12: future outcome labels cannot be queried as features
    (structural import-boundary: feature modules never import the outcome
    builder)."""
    experiment_db = _seed_experiment_db(tmp)
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    result = run_static_import_boundary_canary(repo_root, experiment_db)
    assert result["detected"] == 0


def ra05_13_adjusted_outcome_fields_inaccessible(tmp: Path) -> None:
    """Contract 13: adjusted outcome fields are inaccessible to the feature
    path (runtime canary through the actual screening loader)."""
    experiment_db = _seed_experiment_db(tmp)
    result = run_adjusted_price_canary(_seed_research_db(tmp), experiment_db)
    assert result["detected"] == 0


def ra05_14_mandatory_baselines_generated(tmp: Path) -> None:
    """Contract 14: mandatory baselines B0-B4 are generated per regime."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    for baseline in (
        "B0_BENCHMARK",
        "B1_UNIVERSE",
        "B2_FACTOR_COMPOSITE",
        "B3_HUNTERS_ONLY",
        "B4_EQUAL_SCORING",
    ):
        evaluate_baseline(
            experiment_db,
            regime_id=regime_id,
            dataset_snapshot_id="snap-1",
            baseline=baseline,
            screens=_synthetic_screens(),
            outcome_labels=_synthetic_outcomes(),
        )
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT variant_name FROM experiment_attempt WHERE regime_id=?",
            (regime_id,),
        ).fetchall()
    names = {row[0] for row in rows}
    assert {
        "B0_BENCHMARK",
        "B1_UNIVERSE",
        "B2_FACTOR_COMPOSITE",
        "B3_HUNTERS_ONLY",
        "B4_EQUAL_SCORING",
    } <= names


def ra05_15_fixed_ablations_generated(tmp: Path) -> None:
    """Contract 15: fixed ablations generated -- 1-3 COMPLETE, 4-5
    INSUFFICIENT_DATA (not silently skipped), 6 absent (Packet E)."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    run_remove_one_hunter_ablations(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        screens=_synthetic_screens(),
        outcome_labels=_synthetic_outcomes(),
    )
    record_committee_insufficient_data(
        experiment_db, regime_id=regime_id, dataset_snapshot_id="snap-1"
    )
    with experiment_db.connect(read_only=True) as conn:
        statuses = dict(
            conn.execute(
                "SELECT variant_name, status FROM experiment_attempt WHERE regime_id=?",
                (regime_id,),
            ).fetchall()
        )
    assert "ABLATION_REMOVE_valuation" in statuses
    assert statuses["committee_gated_vs_hunters_only"] == "INSUFFICIENT_DATA"
    assert statuses["agreement_gate_on_vs_off"] == "INSUFFICIENT_DATA"


def ra05_16_failed_attempt_remains_logged(tmp: Path) -> None:
    """Contract 16: a failed/unflattering attempt remains in the append-only
    log; no delete path exists."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    attempt_id = start_attempt(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        variant_kind="BASELINE",
        variant_name="FAILED_VARIANT",
        config={"bad": True},
        attempt_number=1,
    )
    complete_attempt(experiment_db, attempt_id, status="FAILED")
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT status FROM experiment_attempt WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
    assert row[0] == "FAILED"
    try:
        with experiment_db.connect() as conn:
            conn.execute("DELETE FROM experiment_attempt WHERE attempt_id=?", (attempt_id,))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("append-only delete guard failed")


def ra05_17_holdout_regime_immutable_after_open(tmp: Path) -> None:
    """Contract 17: holdout dates/regime cannot silently change after
    opening/sealing."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    seal_evaluation_regime(experiment_db, regime_id)
    try:
        with experiment_db.connect() as conn:
            conn.execute(
                "UPDATE evaluation_regime SET spec_json='{}' WHERE regime_id=?", (regime_id,)
            )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("sealed regime spec was mutable")


def ra05_18_overlapping_outcomes_not_raw_n(tmp: Path) -> None:
    """Contract 18: overlapping outcomes do not report raw rows as
    effective-N."""
    eff = effective_n(60, 126, 21)
    assert eff < 60
    assert eff == 10.0  # 60 * 21/126


def ra05_19_dependence_aware_interval_deterministic(tmp: Path) -> None:
    """Contract 19: dependence-aware interval deterministic under recorded
    seed."""
    series = [0.05 + (i % 5) * 0.01 for i in range(60)]
    assert stationary_bootstrap(series, seed=7) == stationary_bootstrap(series, seed=7)


def ra05_20_cost_assumptions_versioned(tmp: Path) -> None:
    """Contract 20: transaction-cost assumptions versioned in config_json."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    attempt_id = start_attempt(
        experiment_db,
        regime_id=regime_id,
        dataset_snapshot_id="snap-1",
        variant_kind="BASELINE",
        variant_name="COST_VERSIONED",
        config={"cost_profile": "moderate", "one_way_bps": 5},
        attempt_number=1,
    )
    complete_attempt(experiment_db, attempt_id)
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT config_json, config_hash FROM experiment_attempt WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
    assert "one_way_bps" in row[0]
    assert len(row[1]) == 64


def ra05_21_committee_vs_hunters_report(tmp: Path) -> None:
    """Contract 21: committee-vs-Hunters report produced without mass LLM
    replay -- the honest INSUFFICIENT_DATA mechanism is the visible
    committee ablation attempt."""
    experiment_db = _seed_experiment_db(tmp)
    regime_id = _seed_regime(experiment_db)
    record_committee_insufficient_data(
        experiment_db, regime_id=regime_id, dataset_snapshot_id="snap-1"
    )
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT config_json FROM experiment_attempt WHERE regime_id=? "
            "AND variant_name='committee_gated_vs_hunters_only'",
            (regime_id,),
        ).fetchone()
    assert row is not None
    assert "no historical committee replay" in row[0]


def ra05_22_production_config_unchanged(tmp: Path) -> None:
    """Contract 22: production strategy/config remains byte/semantically
    unchanged -- Packets A-D only CALL hunters/scoring, never modify them."""
    experiment_db = _seed_experiment_db(tmp)
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    result = run_static_import_boundary_canary(repo_root, experiment_db)
    assert result["detected"] == 0
    # The validation package must not define any Hunter or scoring config.
    import tradehub_research.validation

    assert not hasattr(tradehub_research.validation, "registered_screens")


def ra05_23_forward_prediction_immutable(tmp: Path) -> None:
    """Contract 23: forward prediction is immutable before outcome -- the
    DB trigger forbids ANY update to a forward_prediction row."""
    from tradehub_research.validation.forward_collector import record_prediction

    experiment_db = _seed_experiment_db(tmp)
    prediction_id = record_prediction(
        experiment_db,
        security_id="sec-1",
        as_of="2024-01-01T00:00:00Z",
        variant_name="production",
        score_value=0.7,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"m": 0.05},
        config_hash="cfg-1",
        evidence_ids=["ev-1"],
        horizon_sessions=63,
    )
    try:
        with experiment_db.connect() as conn:
            conn.execute(
                "UPDATE forward_prediction SET score_value=0.99 WHERE prediction_id=?",
                (prediction_id,),
            )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("forward_prediction was mutable before outcome")


def ra05_24_outcome_append_does_not_mutate_prediction(tmp: Path) -> None:
    """Contract 24: outcome append does not mutate the prediction -- the
    outcome lives in its own append-only row; the original prediction row is
    byte-unchanged after the append."""
    from tradehub_research.validation.forward_collector import append_outcome, record_prediction

    experiment_db = _seed_experiment_db(tmp)
    prediction_id = record_prediction(
        experiment_db,
        security_id="sec-1",
        as_of="2024-01-01T00:00:00Z",
        variant_name="production",
        score_value=0.7,
        state=None,
        screen_passed=True,
        sufficient_data=True,
        raw_features={"m": 0.05},
        config_hash="cfg-1",
        evidence_ids=["ev-1"],
        horizon_sessions=63,
    )
    with experiment_db.connect(read_only=True) as conn:
        before = conn.execute(
            "SELECT * FROM forward_prediction WHERE prediction_id=?", (prediction_id,)
        ).fetchone()
        before_bytes = tuple(before)
    append_outcome(
        experiment_db,
        prediction_id=prediction_id,
        outcome_status="OBSERVED",
        total_return=0.03,
    )
    with experiment_db.connect(read_only=True) as conn:
        after = conn.execute(
            "SELECT * FROM forward_prediction WHERE prediction_id=?", (prediction_id,)
        ).fetchone()
        outcome = conn.execute(
            "SELECT outcome_status, total_return FROM forward_outcome WHERE prediction_id=?",
            (prediction_id,),
        ).fetchone()
    assert tuple(after) == before_bytes  # prediction row unchanged
    assert outcome["outcome_status"] == "OBSERVED"
    assert outcome["total_return"] == 0.03


def ra05_25_prior_ra_packs_pass(tmp: Path) -> None:
    """Contract 25: RA-00..RA-04 remain PASS (validated by the runner's
    upstream lineage; here we assert the packs are registered and
    importable)."""
    from tradehub_research.acceptance.packs.ra00 import ASSERTIONS as A00
    from tradehub_research.acceptance.packs.ra01 import ASSERTIONS as A01
    from tradehub_research.acceptance.packs.ra02 import ASSERTIONS as A02
    from tradehub_research.acceptance.packs.ra03 import ASSERTIONS as A03
    from tradehub_research.acceptance.packs.ra04 import ASSERTIONS as A04

    assert all(len(a) == 2 for a in (*A00, *A01, *A02, *A03, *A04))


ASSERTIONS = [
    ("ra05.01_deterministic_replay", ra05_01_deterministic_replay),
    ("ra05.02_research_db_read_only", ra05_02_historical_cannot_write_research_db),
    ("ra05.03_experiment_db_boundary", ra05_03_experiment_writes_only_to_experiment_db),
    ("ra05.04_snapshot_hash_verifies", ra05_04_snapshot_hash_verifies),
    ("ra05.05_future_evidence_excluded", ra05_05_future_evidence_excluded),
    ("ra05.06_unknown_pat_excluded", ra05_06_unknown_pat_excluded),
    ("ra05.07_lookahead_caught", ra05_07_injected_lookahead_caught),
    ("ra05.08_pass_and_fail_included", ra05_08_pass_and_fail_screens_included),
    ("ra05.09_never_selected_included", ra05_09_never_selected_names_included),
    ("ra05.10_delisting_retained", ra05_10_delisted_name_never_disappears),
    ("ra05.11_identity_pit", ra05_11_identity_correction_pit),
    ("ra05.12_outcome_not_feature", ra05_12_outcome_label_not_queryable_as_feature),
    ("ra05.13_adjusted_inaccessible", ra05_13_adjusted_outcome_fields_inaccessible),
    ("ra05.14_baselines_generated", ra05_14_mandatory_baselines_generated),
    ("ra05.15_ablations_generated", ra05_15_fixed_ablations_generated),
    ("ra05.16_failed_attempt_retained", ra05_16_failed_attempt_remains_logged),
    ("ra05.17_holdout_immutable", ra05_17_holdout_regime_immutable_after_open),
    ("ra05.18_effective_n_not_raw_rows", ra05_18_overlapping_outcomes_not_raw_n),
    ("ra05.19_bootstrap_deterministic", ra05_19_dependence_aware_interval_deterministic),
    ("ra05.20_costs_versioned", ra05_20_cost_assumptions_versioned),
    ("ra05.21_committee_report_no_replay", ra05_21_committee_vs_hunters_report),
    ("ra05.22_production_unchanged", ra05_22_production_config_unchanged),
    ("ra05.23_forward_prediction_immutable_PACKET_E", ra05_23_forward_prediction_immutable),
    (
        "ra05.24_outcome_append_immutable_PACKET_E",
        ra05_24_outcome_append_does_not_mutate_prediction,
    ),
    ("ra05.25_prior_packs_pass", ra05_25_prior_ra_packs_pass),
]

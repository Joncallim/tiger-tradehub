"""Persistence-boundary tests for the inactive headroom evidence loader."""

from __future__ import annotations

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.hunters.valuation_headroom_input import evaluate_persisted_input, load_input


def _insert(store, security_id, evidence_id, fields, *, pat="2025-05-01T16:00:00Z"):
    return store.insert(
        security_id=security_id,
        source_id="fixture-source",
        source_record_id=evidence_id,
        structured_fields=fields,
        extraction_confidence=1.0,
        event_time="2025-05-01T16:00:00Z",
        public_available_time=pat,
        pat_provenance="source_reported",
        ingested_time="2025-06-03T02:00:00Z",
        evidence_id=evidence_id,
    )


def _database(tmp_path, *, price="1", include_coverage=True, split_factors=()):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as db:
        db.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("security-a", "AAA", "NYSE", "AAA", "Tech", None, "SUPPORTED", "2024-01-01", None),
        )
        db.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("fixture-source", "fixture", 1, "test", "source_reported"),
        )
    store = EvidenceStore(database)
    for metric, value in (
        ("net_income", "50"),
        ("operating_cash_flow", "40"),
        ("capex", "10"),
        ("shares_outstanding", "800"),
    ):
        _insert(
            store,
            "security-a",
            metric,
            {
                "record_type": "xbrl_fact",
                "metric": metric,
                "value": value,
                # Actual SEC adapter field names, normalized only by the loader.
                "start": "2024-04-01",
                "end": "2025-03-31",
            },
        )
    _insert(
        store,
        "security-a",
        "price",
        {"record_type": "price_bar", "session_date": "2025-06-02", "close": price},
        pat="2025-06-03T00:20:00Z",
    )
    _insert(
        store,
        "security-a",
        "currency",
        {
            "record_type": "currency_basis",
            "currency": "USD",
            "evidence_ids": [
                "capex",
                "net_income",
                "operating_cash_flow",
                "price",
                "shares_outstanding",
            ],
        },
    )
    split_ids = []
    for index, factor in enumerate(split_factors):
        evidence_id = f"split-{index}"
        split_ids.append(evidence_id)
        _insert(
            store,
            "security-a",
            evidence_id,
            {"record_type": "split", "effective_date": "2025-04-15", "factor": factor},
        )
    if include_coverage:
        _insert(
            store,
            "security-a",
            "split-coverage",
            {
                "record_type": "split_coverage",
                "covered_from": "2025-03-31",
                "covered_through": "2025-06-02",
                "action_evidence_ids": split_ids,
            },
        )
    return database


def test_provenance_complete_quoted_decimals_produce_known_paper_input(tmp_path):
    database = _database(tmp_path)
    loaded = load_input(
        database,
        security_id="security-a",
        evidence_cutoff="2025-06-03T01:00:00Z",
        expected_price_session="2025-06-02",
    )
    result = evaluate_persisted_input(
        database,
        security_id="security-a",
        evidence_cutoff="2025-06-03T01:00:00Z",
        expected_price_session="2025-06-02",
    )
    assert loaded.free_cash_flow_ttm == 30
    assert loaded.operating_cash_flow_lineage.evidence_id == "operating_cash_flow"
    assert loaded.capex_lineage.evidence_id == "capex"
    assert (result.status, result.remaining_opportunity_ppm) == ("KNOWN", 200_000)


def test_float_or_missing_provenance_fails_closed_to_unknown(tmp_path):
    float_price = evaluate_persisted_input(
        _database(tmp_path / "float", price=1.0),
        security_id="security-a",
        evidence_cutoff="2025-06-03T01:00:00Z",
        expected_price_session="2025-06-02",
    )
    missing_coverage = evaluate_persisted_input(
        _database(tmp_path / "missing", include_coverage=False),
        security_id="security-a",
        evidence_cutoff="2025-06-03T01:00:00Z",
        expected_price_session="2025-06-02",
    )
    assert float_price.status == "UNKNOWN"
    assert missing_coverage.status == "UNKNOWN"


def test_individually_negative_splits_cannot_cancel_to_a_known_factor(tmp_path):
    result = evaluate_persisted_input(
        _database(tmp_path, split_factors=("-2", "-0.5")),
        security_id="security-a",
        evidence_cutoff="2025-06-03T01:00:00Z",
        expected_price_session="2025-06-02",
    )
    assert result.status == "UNKNOWN"

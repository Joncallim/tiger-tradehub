from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.validation.coverage_audit import run_coverage_audit


def test_coverage_audit_on_empty_db_reports_zero_evaluable(tmp_path):
    db = ResearchDB(tmp_path / "research.db")
    db.migrate()

    report = run_coverage_audit(database=db)

    assert report["overall_posture"] == "ZERO_EVALUABLE"
    assert report["any_evidence_ingested"] is False
    assert report["any_screens_run"] is False
    assert all(count == 0 for count in report["table_row_counts"].values())
    assert all(posture == "ZERO_EVALUABLE" for posture in report["hunter_family_posture"].values())
    assert report["tiingo_credentials_configured"] is False


def test_coverage_audit_classifies_partial_evidence(tmp_path):
    db = ResearchDB(tmp_path / "research.db")
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
                "AAPL",
                "NASDAQ",
                "Apple Inc.",
                "Technology",
                "Hardware",
                "SUPPORTED",
                "2020-01-01T00:00:00Z",
                None,
            ),
        )
    EvidenceStore(db).insert(
        security_id="sec-1",
        source_id="tiingo_eod",
        structured_fields={"record_type": "price_bar"},
        extraction_confidence=1.0,
        event_time="2025-01-01T00:00:00Z",
        public_available_time="2025-01-01T00:00:00Z",
        pat_provenance="derived_from_index",
        source_record_id="rec-1",
    )

    report = run_coverage_audit(database=db)

    assert report["any_evidence_ingested"] is True
    assert report["hunter_family_posture"]["momentum"] == "EVALUABLE"
    assert report["hunter_family_posture"]["quality"] == "ZERO_EVALUABLE"
    assert report["hunter_family_posture"]["valuation"] == "PARTIAL"

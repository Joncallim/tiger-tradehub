"""Daily-refresh retire behavior (delisted/unresolvable symbols)."""

from __future__ import annotations

from datetime import date, timedelta

from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops import daily_refresh


def _seed(research_db: ResearchDB, age_days: int = 90) -> None:
    research_db.migrate()
    store = EvidenceStore(research_db)
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "S1",
                "TALMF",
                "US",
                "Fuerte Metals Corp",
                "Materials",
                "Metals",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO universe_membership "
            "(security_id,price,market_cap,avg_dollar_volume,price_eligible,"
            "market_cap_eligible,liquidity_eligible,eligible,valid_from,valid_to,"
            "knowledge_time,pat_provenance,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "S1",
                None,
                None,
                None,
                0,
                0,
                0,
                1,
                "2026-01-01T00:00:00Z",
                None,
                "2026-01-01T00:00:00Z",
                "derived_from_index",
                None,
            ),
        )
    bar = (date.today() - timedelta(days=age_days)).isoformat()
    store.insert(
        security_id="S1",
        source_id="tiingo_eod",
        structured_fields={
            "record_type": "price_bar",
            "provider_ticker": "TALMF",
            "session_date": bar,
            "close": 1.0,
        },
        extraction_confidence=1.0,
        event_time=f"{bar}T00:00:00Z",
        public_available_time=f"{bar}T20:15:00Z",
        pat_provenance="derived_from_index",
        source_record_id=f"S1:{bar}:bar",
    )


def test_retire_is_idempotent_and_listed(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_refresh, "RETIRED_FILE", tmp_path / "retired.json")
    daily_refresh._retire("TALMF", "2026-06-08", "delisted")
    daily_refresh._retire("TALMF", "2026-06-08", "delisted")  # idempotent
    assert daily_refresh.retired_tickers() == {"TALMF"}
    import json

    items = json.loads((tmp_path / "retired.json").read_text())
    assert len(items) == 1


def test_maybe_retire_old_bar_retires(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_refresh, "RETIRED_FILE", tmp_path / "retired.json")
    research_db = ResearchDB(tmp_path / "research.db")
    _seed(research_db)
    daily_refresh._maybe_retire(research_db, "TALMF")
    assert "TALMF" in daily_refresh.retired_tickers()


def test_maybe_retire_recent_bar_does_not_retire(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_refresh, "RETIRED_FILE", tmp_path / "retired.json")
    research_db = ResearchDB(tmp_path / "research.db")
    _seed(research_db, age_days=2)  # recent bar -> within the retire gap
    daily_refresh._maybe_retire(research_db, "TALMF")
    assert daily_refresh.retired_tickers() == set()

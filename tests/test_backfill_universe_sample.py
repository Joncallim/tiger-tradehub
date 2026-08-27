from tradehub_research.backfill.security_bootstrap import bootstrap_security_rows
from tradehub_research.backfill.universe_sample import (
    BOOTSTRAP_COHORT_LABEL,
    freeze_universe_sample,
    hash_select,
    load_universe_sample,
    parse_company_tickers,
)
from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import ExperimentDB

POOL_JSON = """{
  "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
  "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
  "2": {"cik_str": 936463, "ticker": "VOO", "title": "Vanguard S&P 500 ETF"},
  "3": {"cik_str": 1318605, "ticker": "TSLA", "title": "Tesla Inc"}
}"""


def test_parse_company_tickers_normalizes_cik():
    rows = parse_company_tickers(POOL_JSON)
    assert len(rows) == 4
    by_ticker = {row["ticker"]: row for row in rows}
    assert by_ticker["AAPL"]["cik"] == "0000320193"  # zero-padded to 10
    assert by_ticker["MSFT"]["cik"] == "0000789019"


def test_hash_select_is_deterministic_and_bounded():
    rows = parse_company_tickers(POOL_JSON)
    first = hash_select(rows, seed=42, size=2)
    second = hash_select(rows, seed=42, size=2)
    assert first == second
    assert len(first) == 2
    assert first != hash_select(rows, seed=43, size=2)  # seed changes selection


def test_freeze_universe_sample_labels_bootstrap_cohort(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    rows = parse_company_tickers(POOL_JSON)

    sample_id, sample = freeze_universe_sample(experiment_db, pool_rows=rows, seed=7, size=2)

    assert sample["cohort_label"] == BOOTSTRAP_COHORT_LABEL
    assert BOOTSTRAP_COHORT_LABEL in sample["algorithm"]
    assert len(sample["selected_tickers"]) == 2
    loaded = load_universe_sample(experiment_db, sample_id)
    assert loaded["selected_count"] == 2


def test_security_bootstrap_inserts_baseline_rows(tmp_path):
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    rows = parse_company_tickers(POOL_JSON)

    counts = bootstrap_security_rows(
        research_db, selected=rows, knowledge_time="2026-08-01T00:00:00Z"
    )

    assert counts["security"] == 4
    assert counts["identity_event"] == 4
    assert counts["membership"] == 4

    with research_db.connect(read_only=True) as conn:
        security_count = conn.execute("SELECT COUNT(*) FROM security").fetchone()[0]
        identity_count = conn.execute(
            "SELECT COUNT(*) FROM security_identity_event WHERE event_type='baseline'"
        ).fetchone()[0]
        membership_count = conn.execute(
            "SELECT COUNT(*) FROM universe_membership WHERE eligible=1"
        ).fetchone()[0]
    assert (security_count, identity_count, membership_count) == (4, 4, 4)

    # Idempotent: re-running adds nothing.
    counts2 = bootstrap_security_rows(
        research_db, selected=rows, knowledge_time="2026-08-01T00:00:00Z"
    )
    assert counts2 == {"security": 0, "identity_event": 0, "membership": 0}

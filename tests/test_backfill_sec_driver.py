"""SEC bulk driver: synthetic companyfacts.zip parse/ingest/ledger contracts."""

from __future__ import annotations

import json
import zipfile

import pytest

from tradehub_research.backfill.sec_driver import (
    companyfacts_json,
    load_cohort_ciks,
    record_attempt,
)
from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import ExperimentDB

COMPANY_FACTS = {
    "0000000001": {
        "cik": 1,
        "entityName": "Aaa Co",
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "label": "Net Income (Loss)",
                    "units": {
                        "USD": [
                            {
                                "accn": "0000000001-26-000001",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-20",
                                "frame": "CY2025",
                                "val": 1000000,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                            }
                        ]
                    },
                },
                "Assets": {
                    "label": "Assets",
                    "units": {
                        "USD": [
                            {
                                "accn": "0000000001-26-000001",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-20",
                                "frame": "CY2025",
                                "val": 5000000,
                                "end": "2025-12-31",
                            }
                        ]
                    },
                },
            }
        },
    },
    "0000000002": {
        "cik": 2,
        "entityName": "Aab Co",
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "label": "Net Income (Loss)",
                    "units": {
                        "USD": [
                            {
                                "accn": "0000000002-26-000001",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-03-01",
                                "frame": "CY2025",
                                "val": -50000,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                            }
                        ]
                    },
                }
            }
        },
    },
}


def _synthetic_zip(tmp_path):
    path = tmp_path / "companyfacts.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for cik, payload in COMPANY_FACTS.items():
            archive.writestr(f"data/{cik}.json", json.dumps(payload))
    return path


def _seed_cohort(experiment_db: ExperimentDB, rows: list[dict[str, str]]) -> None:
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO universe_sample VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "sample-1",
                "url",
                "h" * 64,
                1,
                "sha256(seed+NUL+ticker); BOOTSTRAP_COHORT",
                len(rows),
                json.dumps(rows),
                len(rows),
                "2026-08-27T00:00:00Z",
            ),
        )


def test_load_cohort_ciks_deterministic(tmp_path):
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    _seed_cohort(
        experiment_db,
        [
            {"cik": "0000000002", "ticker": "BB", "title": "B"},
            {"cik": "0000000001", "ticker": "AA", "title": "A"},
        ],
    )
    assert load_cohort_ciks(experiment_db) == ["0000000001", "0000000002"]


def test_companyfacts_json_member_lookup(tmp_path):
    path = _synthetic_zip(tmp_path)
    raw, member = companyfacts_json(path, "0000000001")
    assert member == "data/0000000001.json"
    assert json.loads(raw)["cik"] == 1
    with pytest.raises(KeyError):
        companyfacts_json(path, "0000099999")


def test_run_sec_backfill_ingests_and_records(tmp_path):
    from tradehub_research.adapters.base import FetchResult, ingest_records
    from tradehub_research.adapters.sec import SecAdapter
    from tradehub_research.evidence import EvidenceStore

    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    with research_db.connect() as conn:
        for cik in ("0000000001", "0000000002"):
            conn.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    cik,
                    f"T{cik[-1]}",
                    "US",
                    "co",
                    None,
                    None,
                    "SUPPORTED",
                    "2026-08-27T00:00:00Z",
                    None,
                ),
            )
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("sec_xbrl", "regulatory_filing", 1, "test", "source_reported"),
        )
    _seed_cohort(
        experiment_db,
        [{"cik": c, "ticker": f"T{c[-1]}", "title": "co"} for c in ("0000000001", "0000000002")],
    )
    zip_path = _synthetic_zip(tmp_path)

    adapter = SecAdapter(user_agent="test@example.com", cache_dir=tmp_path / "cache")
    store = EvidenceStore(research_db)
    for cik in ("0000000001", "0000000002"):
        raw, _ = companyfacts_json(zip_path, cik)
        fetched = FetchResult("zip", "2026-08-28T00:00:00Z", 200, {}, raw, zip_path)
        records = adapter.parse_companyfacts(raw, fetched)
        records = [r for r in records if not r.structured_fields.get("dimensions")]
        records = adapter.with_security(records, cik)
        ids = ingest_records(records, store)
        record_attempt(
            experiment_db,
            cik=cik,
            status="SUCCESS",
            http_status=200,
            bytes_count=len(raw),
            error=None,
        )
        assert len(ids) >= 1

    with research_db.connect(read_only=True) as conn:
        facts = conn.execute(
            "SELECT COUNT(*) FROM evidence_event WHERE source_id='sec_xbrl'"
        ).fetchone()[0]
        pat = conn.execute(
            "SELECT public_available_time FROM evidence_event WHERE source_id='sec_xbrl' LIMIT 1"
        ).fetchone()[0]
    assert facts == 3  # 2x NetIncomeLoss + 1x Assets
    # filed 2026-02-20 -> next-day PAT at 00:00 ET == 05:00 UTC (Feb, EDT)
    assert pat == "2026-02-21T05:00:00Z"
    with experiment_db.connect(read_only=True) as conn:
        attempts = conn.execute(
            "SELECT COUNT(*) FROM backfill_attempt WHERE provider='sec'"
        ).fetchone()[0]
    assert attempts == 2

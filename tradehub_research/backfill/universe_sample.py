"""Hash-selected deterministic universe sample (handoff sec 4.1).

SEC's public company_tickers.json (one bounded, cached fetch) is the frozen
candidate pool: ticker + CIK + title in one file, no key required. A fixed,
non-tunable deterministic hash-select (sha256(seed + "\\0" + ticker),
ascending sort, take first N <= 450) picks the bootstrap cohort BEFORE any
price retrieval -- never selected on future outcomes.

The result is labeled BOOTSTRAP_COHORT: it is a present-day sample, NOT a
historical PIT universe. It may only be promoted to a PIT universe if
membership can be reconstructed without future knowledge and delisted
securities are represented appropriately.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from tradehub_research.db import utc_now
from tradehub_research.validation.experiment_db import ExperimentDB

BOOTSTRAP_COHORT_LABEL = "BOOTSTRAP_COHORT"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
MAX_SAMPLE_SIZE = 450  # matches the Tiingo rolling-month bootstrap ceiling


def parse_company_tickers(raw: str) -> list[dict[str, str]]:
    """Parse SEC company_tickers.json into {ticker, cik, title} rows.

    The SEC file is a JSON object keyed "0","1",... with fields ticker,
    title, cik_str (an integer CIK). CIKs are normalized to zero-padded
    10-digit form for the existing SEC/Tiingo adapter conventions."""
    doc = json.loads(raw)
    rows: list[dict[str, str]] = []
    for entry in doc.values():
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        title = str(entry.get("title", "")).strip()
        cik_raw = str(entry.get("cik_str", entry.get("cik", ""))).strip()
        if not ticker or not cik_raw:
            continue
        rows.append({"ticker": ticker, "cik": cik_raw.zfill(10), "title": title})
    rows.sort(key=lambda r: r["ticker"])
    return rows


def hash_select(rows: list[dict[str, str]], *, seed: int, size: int) -> list[dict[str, str]]:
    """Deterministic, non-tunable hash selection: sha256(seed + NUL + ticker),
    ascending sort, take the first ``size``. Same seed+pool -> same list,
    forever."""
    if size <= 0:
        raise ValueError("sample size must be positive")
    selected = sorted(
        rows,
        key=lambda r: hashlib.sha256(f"{seed}\0{r['ticker']}".encode()).hexdigest(),
    )
    return selected[:size]


def freeze_universe_sample(
    experiment_db: ExperimentDB,
    *,
    pool_rows: list[dict[str, str]],
    seed: int,
    size: int,
    source_pool_ref: str = COMPANY_TICKERS_URL,
) -> tuple[str, dict[str, Any]]:
    """Freeze a BOOTSTRAP_COHORT sample into experiment.db BEFORE any price
    retrieval. Returns (sample_id, sample_dict).

    Records the pool content hash + seed + algorithm + selected list --
    immutable (append-only table). The cohort label is explicit in the
    algorithm field; downstream must never treat it as a PIT universe."""
    pool_json = json.dumps(pool_rows, sort_keys=True, separators=(",", ":"))
    pool_hash = hashlib.sha256(pool_json.encode()).hexdigest()
    selected = hash_select(pool_rows, seed=seed, size=size)
    selected_json = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    sample_id = str(uuid.uuid4())
    algorithm = f"sha256(seed+NUL+ticker) ascending take-{size}; {BOOTSTRAP_COHORT_LABEL}"
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO universe_sample VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sample_id,
                source_pool_ref,
                pool_hash,
                seed,
                algorithm,
                size,
                selected_json,
                len(selected),
                utc_now(),
            ),
        )
    return sample_id, {
        "sample_id": sample_id,
        "source_pool_ref": source_pool_ref,
        "pool_content_hash": pool_hash,
        "seed": seed,
        "algorithm": algorithm,
        "selected_count": len(selected),
        "selected_tickers": [row["ticker"] for row in selected],
        "cohort_label": BOOTSTRAP_COHORT_LABEL,
    }


def load_universe_sample(experiment_db: ExperimentDB, sample_id: str) -> dict[str, Any]:
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM universe_sample WHERE sample_id=?", (sample_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown universe_sample: {sample_id}")
    result = dict(row)
    result["selected_tickers"] = [
        item["ticker"] for item in json.loads(result["selected_tickers_json"])
    ]
    return result

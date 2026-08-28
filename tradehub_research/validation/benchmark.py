"""Packet B: benchmark artifact pinning.

Fetches a benchmark return series (Kenneth French US market daily
F-F_Research_Data_Factors_daily or an equivalent approved broad-market
series), parses it deterministically, and pins source/vintage/hashes into
experiment.db benchmark_artifact BEFORE any evaluation uses it. The
artifact hash, not a convenient path, is the oracle: a benchmark can never
be silently swapped mid-experiment (handoff sec 3.4 / 8 B0).

Implementation note: the fetch itself goes through the same NetworkClient
rate-limit/cache-budget machinery as every other provider adapter. Tests
exercise the parser against bundled fixture text; the live fetch is only
invoked by an operator explicitly running the CLI.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from tradehub_research.db import utc_now
from tradehub_research.validation.experiment_db import ExperimentDB

# Kenneth French daily factors: Mkt-RF, SMB, HML, RF per day.
FF_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library/daily_factors.html"
)
PARSER_VERSION = "ff-daily-market-v1"


def parse_ff_daily_factors(raw: str) -> tuple[dict[str, float], str]:
    """Parse the Kenneth French daily factors file.

    Returns (date -> daily total return of the market portfolio
    (Mkt-RF + RF, i.e. the gross US-market daily return), parsed_series_hash).
    The header/footer preamble and the annual-summary tail are skipped;
    rows are 'YYYYMMDD  MktRF  SMB  HML  RF'. Keys are normalized to ISO
    'YYYY-MM-DD' so session-date comparisons in the outcome builder
    (which uses ISO dates) match -- a YYYYMMDD-keyed series would never
    satisfy entry_session < session <= exit_session against ISO dates.
    """
    lines = raw.splitlines()
    rows: dict[str, float] = {}
    for line in lines:
        if not line.strip() or line.startswith((" ", "Mkt-RF", "Annual")):
            continue
        parts = re.split(r"\s+", line.strip())
        if len(parts) < 5:
            continue
        date_token = parts[0]
        if not re.fullmatch(r"\d{8}", date_token):
            continue
        try:
            mkt_rf = float(parts[1])
            rf = float(parts[4])
        except ValueError:
            continue
        iso = f"{date_token[:4]}-{date_token[4:6]}-{date_token[6:8]}"
        rows[iso] = (mkt_rf + rf) / 100.0  # percent -> decimal
    if not rows:
        raise ValueError("no daily factor rows parsed")
    series_hash = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return rows, series_hash


def pin_benchmark_artifact(
    experiment_db: ExperimentDB,
    *,
    source: str,
    source_url: str,
    vintage_label: str,
    raw_content_hash: str,
    parsed_series_hash: str,
    cache_path: str,
) -> str:
    """Record a fetched/parsed benchmark artifact (append-only). Returns the
    benchmark_id."""
    benchmark_id = str(uuid.uuid4())
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO benchmark_artifact VALUES (?,?,?,?,?,?,?,?)",
            (
                benchmark_id,
                source,
                source_url,
                vintage_label,
                raw_content_hash,
                parsed_series_hash,
                cache_path,
                utc_now(),
            ),
        )
    return benchmark_id


def load_benchmark_daily_returns(
    experiment_db: ExperimentDB, benchmark_id: str
) -> dict[str, float]:
    """Load a pinned benchmark's parsed daily returns from its cache path.

    The cache path is the NetworkClient raw cache file (content-addressed);
    re-parsing deterministically reproduces parsed_series_hash, which is
    verified against the artifact row -- a tampered/rotated file fails.
    """
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM benchmark_artifact WHERE benchmark_id=?", (benchmark_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown benchmark_artifact: {benchmark_id}")
    path = Path(row["cache_path"])
    if not path.exists():
        raise ValueError(f"benchmark cache file missing: {path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw_hash = hashlib.sha256(raw.encode()).hexdigest()
    if raw_hash != row["raw_content_hash"]:
        raise ValueError("benchmark raw content hash mismatch -- artifact was tampered")
    rows, parsed_hash = parse_ff_daily_factors(raw)
    if parsed_hash != row["parsed_series_hash"]:
        raise ValueError("benchmark parsed-series hash mismatch -- parser version drift")
    return rows

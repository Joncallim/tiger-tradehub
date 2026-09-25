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
from dataclasses import dataclass
from pathlib import Path

from tradehub_research.db import utc_now
from tradehub_research.validation.experiment_db import ExperimentDB

# Kenneth French daily factors: Mkt-RF, SMB, HML, RF per day. The live
# artifact is the CSV zip (the classic data_library/daily_factors.html
# page was retired in the 2025 site restructure).
FF_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)
PARSER_VERSION = "ff-daily-market-v1"


def parse_ff_daily_factors(raw: str) -> tuple[dict[str, float], str]:
    """Parse the Kenneth French daily factors file.

    Returns (date -> daily total return of the market portfolio
    (Mkt-RF + RF, i.e. the gross US-market daily return), parsed_series_hash).
    The header/footer preamble and the annual-summary tail are skipped;
    rows are 'YYYYMMDD  MktRF  SMB  HML  RF' (whitespace or comma
    separated -- the live CSV zip uses commas). Keys are normalized to ISO
    'YYYY-MM-DD' so session-date comparisons in the outcome builder
    (which uses ISO dates) match -- a YYYYMMDD-keyed series would never
    satisfy entry_session < session <= exit_session against ISO dates.
    """
    lines = raw.splitlines()
    rows: dict[str, float] = {}
    for line in lines:
        if not line.strip() or line.startswith((" ", "Mkt-RF", "Annual")):
            continue
        parts = re.split(r"[,\s]+", line.strip())
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


@dataclass(frozen=True)
class BenchmarkSeries:
    """A pinned benchmark vintage, loaded and hash-verified."""

    benchmark_id: str
    vintage_label: str
    source: str
    series: dict[str, float]

    @property
    def last_session(self) -> str:
        """The last session the vintage covers (ISO date)."""
        return max(self.series)


def resolve_benchmark_cache_path(cache_path: str | Path, raw_cache_dir: Path) -> Path:
    """Resolve a pinned artifact's recorded cache path against the LIVE cache root.

    Artifacts pinned before the research-data migration record a path INSIDE THE
    OLD CHECKOUT (``data/research/raw/benchmark/ff_daily_factors.csv``), a tree
    that no longer exists -- so a loader that only joins the recorded string can
    never find the file again (the live defect: the weekly report's Benchmark and
    Relative fields could not populate even once the field was wired).

    The cache file is content-addressed and hash-verified on load, so rebasing the
    tail after the ``raw`` component cannot silently substitute a different
    series: a wrong or tampered file fails the hash check instead.
    """
    recorded = Path(cache_path)
    if recorded.is_absolute() and recorded.exists():
        return recorded
    parts = list(recorded.parts)
    if "raw" in parts:
        # LAST occurrence: the pre-migration path carries the checkout prefix too.
        tail = parts[len(parts) - 1 - parts[::-1].index("raw") + 1 :]
    else:
        tail = parts[-2:] if len(parts) >= 2 else parts
    return Path(raw_cache_dir).joinpath(*tail)


def latest_benchmark_artifact(experiment_db: ExperimentDB) -> dict | None:
    """The newest pinned benchmark artifact row, or None when nothing is pinned."""
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM benchmark_artifact ORDER BY retrieved_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row is not None else None


def load_latest_benchmark(experiment_db: ExperimentDB, raw_cache_dir: Path) -> BenchmarkSeries:
    """Load the newest pinned vintage, hash-verified. Raises ValueError if unusable.

    Verification order mirrors ``load_benchmark_daily_returns``: the raw content
    hash is checked BEFORE parsing, then the parsed-series hash, so a rotated or
    tampered cache file is refused rather than silently priced.
    """
    row = latest_benchmark_artifact(experiment_db)
    if row is None:
        raise ValueError("no benchmark_artifact is pinned")
    path = resolve_benchmark_cache_path(row["cache_path"], Path(raw_cache_dir))
    if not path.exists():
        raise ValueError(f"benchmark cache file missing: {path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    if hashlib.sha256(raw.encode()).hexdigest() != row["raw_content_hash"]:
        raise ValueError("benchmark raw content hash mismatch -- artifact was tampered")
    series, parsed_hash = parse_ff_daily_factors(raw)
    if parsed_hash != row["parsed_series_hash"]:
        raise ValueError("benchmark parsed-series hash mismatch -- parser version drift")
    return BenchmarkSeries(
        benchmark_id=str(row["benchmark_id"]),
        vintage_label=str(row["vintage_label"]),
        source=str(row["source"]),
        series=series,
    )


def window_return_pct(series: dict[str, float], start: str, end: str) -> float | None:
    """Compounded benchmark return over a window, as a PERCENT, or None.

    ``start``/``end`` are ISO calendar dates. The base is the last session on or
    before ``start`` and the close is the last session on or before ``end`` -- the
    same comparison the broker history uses for the portfolio's own week, so the
    benchmark and the portfolio are measured over one window.

    A vintage that does not cover BOTH endpoints cannot price the window and
    returns None. It is never extrapolated from a stale series, and a missing
    benchmark must render as ``unavailable`` (with the reason), not as 0.00%.
    """
    if not series or start >= end:
        return None
    sessions = sorted(series)
    base = [session for session in sessions if session <= start]
    close = [session for session in sessions if session <= end]
    if not base or not close:
        return None
    base_session, close_session = base[-1], close[-1]
    if base_session >= close_session:
        return None
    factor = 1.0
    for session in sessions:
        if base_session < session <= close_session:
            factor *= 1.0 + series[session]
    return (factor - 1.0) * 100.0


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

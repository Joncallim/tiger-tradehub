"""Bounded SEC bulk backfill driver for the BOOTSTRAP_COHORT CIKs (#38).

Bulk route: downloads ``https://www.sec.gov/files/companyfacts.zip`` ONCE
(streaming, bounded, cached with the SEC User-Agent
``TigerTradeHub joncallim@gmail.com``), then parses only the frozen cohort
CIKs' XBRL fact payloads through the EXISTING ``SecAdapter.parse_companyfacts``
(unmodified), binds canonical security identity via ``with_security``, and
ingests through the production evidence store.

PAT discipline (owner brief section 4): every fact's public availability is
the SEC filing date + 1 day (``derived_from_index``, ``day_precision`` --
conservative; EDGAR publishes filings on the filing date, +1d can never leak).
No PAT is guessed; facts without a defensible filed date are dropped as
NOT_EVALUABLE. No paid fundamentals. No purchases.

Dimensioned (segment) facts are excluded from the entity-level evidence
stream: the Hunters' concept aliases are entity-level, and a segment fact
must never win a TTM/instant selection against the total.

Every attempt is recorded append-only in experiment.db ``backfill_attempt``
(provider='sec'). Resume-safe: CIKs with ingested ``sec_xbrl`` evidence are
skipped. The cohort is never resampled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from tradehub_research.adapters.base import FetchResult, ingest_records
from tradehub_research.adapters.sec import SecAdapter
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.validation.experiment_db import (
    DEFAULT_EXPERIMENT_DB_PATH,
    ExperimentDB,
)

COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SUBMISSIONS_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
MAX_ZIP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB hard ceiling (2026 artifact >1 GiB)
DOWNLOAD_TIMEOUT = httpx.Timeout(1800.0, connect=30.0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def record_attempt(
    experiment_db: ExperimentDB,
    *,
    cik: str,
    status: str,
    http_status: int | None,
    bytes_count: int | None,
    error: str | None,
) -> None:
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO backfill_attempt "
            "(attempt_id, provider, symbol_or_cik, status, http_status, bytes, error, "
            "requested_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "sec",
                cik,
                status,
                http_status,
                bytes_count,
                error,
                _utc_now(),
            ),
        )


def load_cohort_ciks(experiment_db: ExperimentDB) -> list[str]:
    """Frozen cohort CIKs (zero-padded), deterministic sorted order."""
    with experiment_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT selected_tickers_json, selected_count FROM universe_sample ORDER BY created_at"
        ).fetchall()
    if not rows:
        raise RuntimeError("no frozen universe_sample in experiment.db")
    sample = rows[-1]
    tickers = json.loads(sample["selected_tickers_json"])
    if len(tickers) != sample["selected_count"]:
        raise RuntimeError("universe_sample count mismatch -- corrupt sample, refusing to continue")
    ciks = sorted({str(row["cik"]).zfill(10) for row in tickers})
    return ciks


def cik_has_evidence(research_db: ResearchDB, cik: str) -> bool:
    with research_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM evidence_event WHERE security_id=? AND source_id='sec_xbrl' LIMIT 1",
            (cik,),
        ).fetchone()
    return row is not None


def download_companyfacts(cache_dir: Path, user_agent: str) -> tuple[Path, str]:
    """Stream the bulk zip once into the adapter cache; returns (path, sha256)."""
    target = cache_dir / "sec-bulk" / "companyfacts.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return target, digest
    partial = target.with_suffix(".zip.part")
    headers = {"User-Agent": user_agent}
    digest = hashlib.sha256()
    with httpx.Client(headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        with client.stream("GET", COMPANYFACTS_URL) as response:
            response.raise_for_status()
            total = 0
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ZIP_BYTES:
                        partial.unlink(missing_ok=True)
                        raise RuntimeError("companyfacts.zip exceeds the 1 GiB safety ceiling")
                    digest.update(chunk)
                    handle.write(chunk)
    partial.rename(target)
    return target, digest.hexdigest()


def companyfacts_json(zip_path: Path, cik: str) -> tuple[bytes, str]:
    """(raw json bytes, member name) for one cohort CIK; raises KeyError when absent.

    The bulk artifact's member layout has changed over time: both
    ``data/<cik>.json`` (classic) and ``CIK<cik>.json`` (current) are
    tried; the member actually used is returned for lineage.
    """
    candidates = (f"data/{cik}.json", f"CIK{cik}.json")
    with zipfile.ZipFile(zip_path) as archive:
        for member in candidates:
            try:
                return archive.read(member), member
            except KeyError:
                continue
    raise KeyError(f"no companyfacts member for CIK {cik} (tried {', '.join(candidates)})")


def run_sec_backfill(*, settings: ResearchSettings, experiment_db: ExperimentDB) -> int:
    if not settings.sec_user_agent:
        raise SystemExit("RESEARCH_SEC_USER_AGENT is required for SEC EDGAR")
    research_db = ResearchDB(settings.db_path, settings.busy_timeout_ms)
    ciks = load_cohort_ciks(experiment_db)
    print(json.dumps({"phase": "plan", "cohort_ciks": len(ciks)}, sort_keys=True))

    zip_path, zip_digest = download_companyfacts(
        settings.adapter_cache_dir, settings.sec_user_agent
    )
    print(
        json.dumps(
            {"phase": "downloaded", "bytes": zip_path.stat().st_size, "sha256": zip_digest},
            sort_keys=True,
        )
    )

    adapter = SecAdapter(user_agent=settings.sec_user_agent, cache_dir=settings.adapter_cache_dir)
    store = EvidenceStore(research_db)
    summary = {"SUCCESS": 0, "ERROR": 0, "SKIPPED_DONE": 0}
    retrieved_at = _utc_now()
    zip_url = f"{COMPANYFACTS_URL}#sha256={zip_digest}"

    for cik in ciks:
        if cik_has_evidence(research_db, cik):
            summary["SKIPPED_DONE"] += 1
            continue
        raw = b""
        try:
            raw, member = companyfacts_json(zip_path, cik)
        except KeyError:
            record_attempt(
                experiment_db,
                cik=cik,
                status="ERROR",
                http_status=None,
                bytes_count=None,
                error="NOT_IN_COMPANYFACTS: no companyfacts member for this CIK",
            )
            summary["ERROR"] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            record_attempt(
                experiment_db,
                cik=cik,
                status="ERROR",
                http_status=None,
                bytes_count=None,
                error=f"ZIP: {type(exc).__name__}: {str(exc)[:200]}",
            )
            summary["ERROR"] += 1
            continue
        try:
            fetched = FetchResult(
                zip_url, retrieved_at, 200, {"content-type": "application/json"}, raw, zip_path
            )
            records = adapter.parse_companyfacts(raw, fetched)
            # Entity-level facts only: dimensioned (segment) facts must never
            # participate in entity-level TTM/instant selection.
            records = [r for r in records if not r.structured_fields.get("dimensions")]
            if not records:
                record_attempt(
                    experiment_db,
                    cik=cik,
                    status="SUCCESS",
                    http_status=200,
                    bytes_count=len(raw),
                    error="EMPTY: no aliased entity-level facts in companyfacts",
                )
                summary["SUCCESS"] += 1
                continue
            records = adapter.with_security(records, cik)
            ingest_records(records, store)
            record_attempt(
                experiment_db,
                cik=cik,
                status="SUCCESS",
                http_status=200,
                bytes_count=len(raw),
                error=None,
            )
            summary["SUCCESS"] += 1
        except Exception as exc:  # noqa: BLE001
            record_attempt(
                experiment_db,
                cik=cik,
                status="ERROR",
                http_status=None,
                bytes_count=len(raw) if raw else None,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            summary["ERROR"] += 1

    print(
        json.dumps({"phase": "done", "summary": summary, "zip_sha256": zip_digest}, sort_keys=True)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sec-backfill")
    parser.add_argument("--experiment-db", type=Path, default=DEFAULT_EXPERIMENT_DB_PATH)
    args = parser.parse_args(argv)
    settings = ResearchSettings()
    experiment_db = ExperimentDB(args.experiment_db)
    experiment_db.migrate()
    return run_sec_backfill(settings=settings, experiment_db=experiment_db)


if __name__ == "__main__":
    raise SystemExit(main())

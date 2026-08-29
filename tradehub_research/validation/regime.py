"""Packet A: draft/seal an evaluation_regime from a snapshot's evidence/
universe DATE RANGE only -- never touches performance data (handoff sec 11.3).

A default holdout selection:
- label cutoff = snapshot end minus max evaluated horizon (252 sessions)
- holdout = latest ~20% of eligible evaluation dates before that cutoff,
  subject to a reasonable minimum duration
- exact dates are recorded and never silently moved

If coverage is too short, this raises InsufficientCoverageError -- the
regime is simply not drafted; callers should report INSUFFICIENT DATA
rather than shrinking the holdout to force a result (handoff sec 11.3).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Any

from tradehub_research.db import ResearchDB, utc_now

MAX_HORIZON_SESSIONS = 252
# ~1 trading year at ~252 sessions; used only to translate a coverage window
# into an approximate calendar-day minimum for the "reasonable minimum
# duration" holdout guard below. This is a coarse, documented approximation
# -- Packet C's monthly PIT grid is the authoritative session enumeration.
_APPROX_SESSIONS_PER_CALENDAR_YEAR = 252
_APPROX_CALENDAR_DAYS_PER_SESSION = 365.25 / _APPROX_SESSIONS_PER_CALENDAR_YEAR
MIN_HOLDOUT_CALENDAR_DAYS = 90
MIN_DEVELOPMENT_CALENDAR_DAYS = 90


class InsufficientCoverageError(ValueError):
    """Raised when the coverage window is too short to draft a regime."""


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def draft_evaluation_regime(
    experiment_db: ResearchDB,
    dataset_snapshot_id: str,
    *,
    coverage_start: str,
    coverage_end: str,
    max_horizon_sessions: int = MAX_HORIZON_SESSIONS,
    fold_months: int = 6,
) -> str:
    """Draft (unsealed) evaluation_regime purely from a date range.

    Raises InsufficientCoverageError if the resulting development+holdout
    windows would be too short to be meaningful -- this is the deliberate
    "stop and report INSUFFICIENT DATA rather than shrink" behavior the
    handoff requires (section 11.3): callers must not retry with a smaller
    minimum to force a regime through.
    """
    start = _parse_date(coverage_start)
    end = _parse_date(coverage_end)
    if end <= start:
        raise InsufficientCoverageError(f"coverage_end {end} is not after coverage_start {start}")

    approx_horizon_days = round(max_horizon_sessions * _APPROX_CALENDAR_DAYS_PER_SESSION)
    label_cutoff = end - timedelta(days=approx_horizon_days)
    if label_cutoff <= start:
        raise InsufficientCoverageError(
            f"coverage window [{start},{end}] is shorter than the max evaluated "
            f"horizon ({max_horizon_sessions} sessions ~= {approx_horizon_days} days); "
            "no observation in this window has a matured label. INSUFFICIENT DATA."
        )

    matured_span_days = (label_cutoff - start).days
    holdout_days = max(round(matured_span_days * 0.2), MIN_HOLDOUT_CALENDAR_DAYS)
    holdout_start = label_cutoff - timedelta(days=holdout_days)
    development_days = (holdout_start - start).days

    if holdout_days > matured_span_days or development_days < MIN_DEVELOPMENT_CALENDAR_DAYS:
        raise InsufficientCoverageError(
            f"matured coverage span ({matured_span_days} days, ending {label_cutoff}) is too "
            f"short to carve out a >= {MIN_HOLDOUT_CALENDAR_DAYS}-day holdout AND a "
            f"development window; got development={development_days} days. "
            "INSUFFICIENT DATA -- do not shrink the minimums to force a regime."
        )

    spec = {
        "dataset_snapshot_id": dataset_snapshot_id,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "max_horizon_sessions": max_horizon_sessions,
        "fold_months": fold_months,
        "development_window": {"start": str(start), "end": str(holdout_start)},
        "holdout_window": {"start": str(holdout_start), "end": str(label_cutoff)},
        "label_maturity_cutoff": str(label_cutoff),
        "unmatured_tail": {"start": str(label_cutoff), "end": str(end)},
    }
    spec_json = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    spec_hash = hashlib.sha256(spec_json.encode()).hexdigest()
    regime_id = str(uuid.uuid4())

    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO evaluation_regime "
            "(regime_id,dataset_snapshot_id,spec_json,spec_hash,sealed_at,created_at) "
            "VALUES (?,?,?,?,NULL,?)",
            (regime_id, dataset_snapshot_id, spec_json, spec_hash, utc_now()),
        )
    return regime_id


def load_evaluation_regime(experiment_db: ResearchDB, regime_id: str) -> dict[str, Any]:
    with experiment_db.connect(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM evaluation_regime WHERE regime_id=?", (regime_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown evaluation_regime: {regime_id}")
    result = dict(row)
    result["spec"] = json.loads(result["spec_json"])
    return result


def seal_evaluation_regime(experiment_db: ResearchDB, regime_id: str) -> None:
    """One-time seal transition -- dates never change after this.

    After sealing, the schema's own trigger (experiment_attempt_seal_guard)
    blocks any non-HOLDOUT experiment_attempt insert for this regime; only
    the single pre-registered final HOLDOUT evaluation may run (handoff
    section 11.3, RA-05 contract #17).
    """
    with experiment_db.connect() as conn:
        cursor = conn.execute(
            "UPDATE evaluation_regime SET sealed_at=? WHERE regime_id=? AND sealed_at IS NULL",
            (utc_now(), regime_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"regime {regime_id} is already sealed or does not exist; "
                "sealing is a one-time transition"
            )

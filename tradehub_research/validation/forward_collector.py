"""Packet E: forward tracker -- the first-class Phase-5 output.

Every future decision/screen state is recorded as an IMMUTABLE
forward_prediction BEFORE the outcome exists, with raw/versioned features,
evidence/config hashes, and pass/fail/sufficient status for EVERY screened
security (including FAILs -- the broad screened population, never just
chosen candidates). Later, outcomes are APPENDED as forward_outcome rows
referencing the prediction; the original prediction is never edited.

This is the mechanism that guarantees "we never again lack the data
required to evaluate TradeHub": from the moment the tracker ships, the
system continuously retains the full screened population and its
1m/3m/6m/12m outcomes (handoff sec 16, steering directive).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Any

from tradehub_research.db import utc_now
from tradehub_research.validation.experiment_db import ExperimentDB

HORIZON_SESSIONS = (21, 63, 126, 252)
_SESSIONS_PER_DAY = 252 / 365.25


def record_prediction(
    experiment_db: ExperimentDB,
    *,
    security_id: str,
    as_of: str,
    variant_name: str,
    score_value: float | None,
    state: str | None,
    screen_passed: bool | None,
    sufficient_data: bool | None,
    raw_features: dict[str, Any],
    config_hash: str,
    evidence_ids: list[str],
    horizon_sessions: int,
) -> str:
    """Record ONE immutable forward prediction BEFORE its outcome exists.

    Idempotent: identical (security, as_of, variant, horizon) predictions
    dedupe (UNIQUE constraint) -- the original row is never overwritten.
    Returns the prediction_id (existing on dedupe).
    """
    if horizon_sessions not in HORIZON_SESSIONS:
        raise ValueError(f"horizon_sessions must be one of {HORIZON_SESSIONS}")
    raw_features_hash = hashlib.sha256(
        json.dumps(raw_features, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prediction_id = str(uuid.uuid4())
    outcome_due = _outcome_due_date(as_of, horizon_sessions)
    with experiment_db.connect() as conn:
        existing = conn.execute(
            "SELECT prediction_id FROM forward_prediction "
            "WHERE security_id=? AND as_of=? AND variant_name=? AND horizon_sessions=?",
            (security_id, as_of[:10], variant_name, horizon_sessions),
        ).fetchone()
        if existing is not None:
            return str(existing[0])
        conn.execute(
            "INSERT INTO forward_prediction VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                prediction_id,
                security_id,
                as_of[:10],
                variant_name,
                score_value,
                state,
                int(screen_passed) if screen_passed is not None else None,
                int(sufficient_data) if sufficient_data is not None else None,
                raw_features_hash,
                config_hash,
                json.dumps(sorted(evidence_ids), separators=(",", ":")),
                horizon_sessions,
                outcome_due,
                utc_now(),
            ),
        )
    return prediction_id


def record_prediction_from_screen(
    experiment_db: ExperimentDB,
    *,
    screen: dict[str, Any],
    horizon_sessions: int,
    variant_name: str = "production",
) -> str:
    """Record a forward prediction from ONE replayed/production screen_result
    row (raw features + evidence ids + pass/fail + config hash), for EVERY
    screened security -- including FAILs and insufficient-data rows."""
    raw_features = json.loads(screen.get("raw_features_json", "{}"))
    evidence_ids = json.loads(screen.get("evidence_ids_json", "[]"))
    return record_prediction(
        experiment_db,
        security_id=screen["security_id"],
        as_of=screen.get("computed_at", screen.get("run_id", "")),
        variant_name=variant_name,
        score_value=float(screen.get("confidence", 0.0) or 0.0),
        state=None,
        screen_passed=bool(screen.get("passed")),
        sufficient_data=bool(screen.get("sufficient_data")),
        raw_features=raw_features,
        config_hash=screen.get("config_hash", ""),
        evidence_ids=evidence_ids,
        horizon_sessions=horizon_sessions,
    )


def record_all_screen_predictions(
    experiment_db: ExperimentDB,
    *,
    screens: list[dict[str, Any]],
    horizons: tuple[int, ...] = HORIZON_SESSIONS,
) -> dict[str, int]:
    """Record forward predictions for the ENTIRE screened population at all
    four horizons (pass AND fail, sufficient AND insufficient)."""
    counts: dict[str, int] = {}
    for screen in screens:
        for horizon in horizons:
            prediction_id = record_prediction_from_screen(
                experiment_db, screen=screen, horizon_sessions=horizon
            )
            counts.setdefault(screen["security_id"], 0)
            counts[screen["security_id"]] += 1 if prediction_id else 0
    return counts


def append_outcome(
    experiment_db: ExperimentDB,
    *,
    prediction_id: str,
    outcome_status: str,
    raw_return: float | None = None,
    total_return: float | None = None,
    benchmark_relative_return: float | None = None,
    entry_session_date: str | None = None,
    exit_session_date: str | None = None,
) -> str:
    """Append an eventual outcome to an existing prediction.

    The prediction row is NEVER mutated (DB trigger forbids UPDATE); the
    outcome is a separate append-only row referencing it. At most one
    outcome per prediction (UNIQUE(prediction_id))."""
    outcome_id = str(uuid.uuid4())
    with experiment_db.connect() as conn:
        conn.execute(
            "INSERT INTO forward_outcome VALUES (?,?,?,?,?,?,?,?,?)",
            (
                outcome_id,
                prediction_id,
                outcome_status,
                raw_return,
                total_return,
                benchmark_relative_return,
                entry_session_date,
                exit_session_date,
                utc_now(),
            ),
        )
    return outcome_id


def _outcome_due_date(as_of: str, horizon_sessions: int) -> str:
    day = date.fromisoformat(as_of[:10])
    days = round(horizon_sessions / _SESSIONS_PER_DAY)
    return (day + timedelta(days=days)).isoformat()

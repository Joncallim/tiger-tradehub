"""Canonical portfolio state machine: derivation, persistence, settlement.

Current state is ALWAYS derived from the immutable transition ledger (latest
``effective_at`` at or before the decision moment), never stored as mutable
state.  Persistence is derived from append-only observations; repeated scoring
over unchanged evidence never counts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from tradehub_research.portfolio.types import (
    PENDING_STATES,
    State,
)

TRANSITION_CAUSES = (
    "RULE_PERSISTED",
    "MATERIAL_CHANGE",
    "VERIFIED_THESIS_BREAK",
    "SETTLEMENT",
    "COOLDOWN",
)


def parse_date_only(value: str) -> date:
    """Extract the UTC calendar date from an RFC3339 timestamp."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def current_state(db: Any, security_id: str, as_of: str) -> dict[str, Any]:
    """Latest transition effective at or before ``as_of``; default DISCOVER.

    Returns dict with keys: state, transition_id, effective_at, decision_id
    (transition fields None for the implicit DISCOVER default).
    """
    row = db.execute(
        "SELECT to_state,transition_id,effective_at,decision_id "
        "FROM portfolio_state_transition WHERE security_id=? AND effective_at<=? "
        "ORDER BY effective_at DESC,transition_id DESC LIMIT 1",
        (security_id, as_of),
    ).fetchone()
    if row is None:
        return {
            "state": State.DISCOVER,
            "transition_id": None,
            "effective_at": None,
            "decision_id": None,
        }
    return {
        "state": State(row["to_state"]),
        "transition_id": row["transition_id"],
        "effective_at": row["effective_at"],
        "decision_id": row["decision_id"],
    }


def cooldown_satisfied(
    state_entry_effective_at: str | None,
    as_of: str,
    cooldown_days: int,
) -> bool:
    """Cooldown is inclusive at the exact second boundary (RFC3339 strings)."""
    if cooldown_days <= 0:
        return True
    if state_entry_effective_at is None:
        return True
    entry = datetime.fromisoformat(state_entry_effective_at.replace("Z", "+00:00"))
    moment = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    return moment >= entry + timedelta(days=cooldown_days)


def persistence_count(
    db: Any,
    security_id: str,
    policy_version: str,
    as_of: str,
    current_state_value: State,
    candidate_state: State,
    decision_id: str,
    score_evidence_hash: str | None,
    signal_status: str,
    signal_state: State,
    *,
    hypothetical_evidence_driven: bool = True,
    min_interval_calendar_days: int = 0,
) -> int:
    """Consecutive qualifying evidence-driven observations ending now.

    Only distinct ``scored_evidence_hash`` rows with ``change_cause``
    EVIDENCE_DRIVEN under the same policy and state epoch count.  Model
    reruns, rebases, corrections, and unchanged evidence neither increment
    nor reset.  The hypothetical current observation is included ONLY when
    the current observation itself is evidence-driven — a rebase with a
    fresh hash must never masquerade as persistence.
    """
    if score_evidence_hash is None:
        return 0
    epoch = db.execute(
        "SELECT max(effective_at) FROM portfolio_state_transition "
        "WHERE security_id=? AND to_state=? AND effective_at<=?",
        (security_id, current_state_value.value, as_of),
    ).fetchone()[0]
    raw = db.execute(
        "SELECT observed_at,decision_id,scored_evidence_hash,signal_state,signal_status "
        "FROM portfolio_state_observation "
        "WHERE security_id=? AND policy_version=? AND evidence_driven=1 "
        "AND observed_at>? AND observed_at<?",
        (
            security_id,
            policy_version,
            epoch or "",
            as_of,
        ),
    ).fetchall()
    rows: list[dict[str, Any]] = [dict(row) for row in raw]
    if hypothetical_evidence_driven:
        rows.append(
            {
                "observed_at": as_of,
                "decision_id": decision_id,
                "scored_evidence_hash": score_evidence_hash,
                "signal_state": signal_state.value,
                "signal_status": signal_status,
            }
        )
    rows.sort(key=lambda r: (r["observed_at"], r["decision_id"]))
    if min_interval_calendar_days > 0:
        # scheduled-observation cadence: at most one counted observation per
        # interval, so rapid same-day reruns cannot manufacture persistence
        kept: list[dict[str, Any]] = []
        last_kept_at: str | None = None
        for row in rows:
            if (
                last_kept_at is None
                or _calendar_gap(last_kept_at, row["observed_at"]) >= min_interval_calendar_days
            ):
                kept.append(row)
                last_kept_at = row["observed_at"]
        rows = kept
    seen_hashes: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        evidence_hash = row["scored_evidence_hash"]
        if evidence_hash is not None and evidence_hash in seen_hashes:
            continue
        if evidence_hash is not None:
            seen_hashes.add(evidence_hash)
        deduped.append(row)
    count = 0
    for row in reversed(deduped):
        if row["signal_status"] == "PASS" and row["signal_state"] == candidate_state.value:
            count += 1
        else:
            break
    return count


def _calendar_gap(start_iso: str, end_iso: str) -> int:
    """Whole UTC calendar days between two ISO timestamps (date difference)."""
    from datetime import datetime

    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return (end.date() - start.date()).days


def pending_resolution(
    db: Any,
    security_id: str,
    current: dict[str, Any],
    trusted_quantity_microunits: int | None,
    as_of: str,
    quantity_tolerance_microunits: int,
    pending_max_calendar_days: int,
    quantity_status: str = "KNOWN",
) -> tuple[str, bool, str | None]:
    """Resolve a pending recommendation state against a trusted snapshot.

    Returns (outcome, satisfied, reason) where outcome is one of:
      SETTLE_HOLD / SETTLE_WATCH / TRIM_EXIT_CANDIDATE / PENDING_STALE /
      STILL_PENDING.  ``satisfied`` means the fill quantity test passed.
    A pending state is never settled from an UNKNOWN/STALE holding quantity:
    that would let stale data manufacture a state change.
    """
    state = current["state"]
    if state not in PENDING_STATES:
        return "NOT_PENDING", False, None
    if quantity_status != "KNOWN":
        return "STILL_PENDING", False, "quantity_status_not_known"
    decision_id = current["decision_id"]
    proposal = None
    if decision_id is not None:
        proposal = db.execute(
            "SELECT * FROM trade_proposal WHERE decision_id=?", (decision_id,)
        ).fetchone()
    if proposal is None:
        # A pending state without a recoverable originating proposal cannot be
        # settled by quantity; staleness is the only exit.
        if pending_max_calendar_days > 0 and current["effective_at"] is not None:
            age = _calendar_age(current["effective_at"], as_of)
            if age > pending_max_calendar_days:
                reason = "pending_stale"
                if state == State.EXIT:
                    return "SETTLE_WATCH", False, reason
                return "SETTLE_HOLD", False, reason
        return "STILL_PENDING", False, None
    completion = int(proposal["completion_quantity_microunits"])
    if trusted_quantity_microunits is None:
        return "STILL_PENDING", False, None
    if state == State.EXIT:
        filled = trusted_quantity_microunits <= completion + quantity_tolerance_microunits
        if filled:
            return "SETTLE_WATCH", True, None
    elif state == State.TRIM:
        filled = trusted_quantity_microunits <= completion + quantity_tolerance_microunits
        if filled:
            return "TRIM_EXIT_CANDIDATE", True, None
    else:  # ENTER / ADD
        filled = trusted_quantity_microunits >= completion - quantity_tolerance_microunits
        if filled:
            return "SETTLE_HOLD", True, None
    if pending_max_calendar_days > 0 and current["effective_at"] is not None:
        age = _calendar_age(current["effective_at"], as_of)
        if age > pending_max_calendar_days:
            reason = "pending_stale"
            if state == State.EXIT:
                return "SETTLE_WATCH", False, reason
            return "SETTLE_HOLD", False, reason
    return "STILL_PENDING", False, None


def _calendar_age(effective_at: str, as_of: str) -> int:
    start = datetime.fromisoformat(effective_at.replace("Z", "+00:00")).date()
    end = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    return (end - start).days

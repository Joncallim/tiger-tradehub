"""Packet B: outcome-label builder with conservative entry/exit semantics.

Entry convention (handoff sec 6.3): the FIRST eligible session after the
observation timestamp (never a price knowable at decision time). Prefer
next-session OPEN; fall back to next-session CLOSE with an explicit
convention label. Exit is the session `horizon` sessions later, at close.

Horizons: 1m=21, 3m=63, 6m=126, 12m=252 sessions. Co-primary 63/126.

Delisting/corporate-action rules (handoff sec 3.3 / 15): a delisted name
NEVER disappears. If a delisting event is visible before the exit session
and no terminal payoff exists, the label is DELISTING_OUTCOME_UNKNOWN,
retained in coverage/censoring statistics. No zero-imputation, no silent
forward-fill, no pretending the missing terminal return is zero.

The outcome builder reads research.db (or a frozen snapshot) READ-ONLY and
writes only to experiment.db's outcome_label (append-only).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.portfolio.prices import (
    _action_records,
    _cumulative_adjustments,
    _visible_records,
    next_session_on_or_after,
)

HORIZON_SESSIONS = (21, 63, 126, 252)
BUILDER_VERSION = "outcome-builder-v1"

DECIMAL_ZERO = Decimal(0)


def _d(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _quantize(value: Decimal) -> float:
    return float(value.quantize(Decimal("1e-12"), rounding=ROUND_HALF_UP))


def _delisting_info(db: Any, security_id: str, as_of: str) -> tuple[str | None, str | None]:
    """Return (delisting_event_ref, delisted_date) visible at as_of, if any.

    Outcome-side: uses the realized visibility bound (like entry/exit
    prices) -- a delisting that happened after the observation but before
    the exit session must still classify the label correctly. The
    decision-time feature path never sees this (lookahead canaries guard it).
    """
    rows = db.execute(
        "SELECT id, event_time, public_available_time "
        "FROM security_identity_event WHERE security_id=? AND event_type='delisting' "
        "AND public_available_time IS NOT NULL AND public_available_time <= ? "
        "ORDER BY public_available_time, id",
        (security_id, as_of),
    ).fetchall()
    if not rows:
        return None, None
    row = rows[0]
    return str(row["id"]), str(row["event_time"])[:10]


def _security_delisted_at(db: Any, security_id: str) -> str | None:
    row = db.execute(
        "SELECT delisted_at FROM security WHERE security_id=?", (security_id,)
    ).fetchone()
    if row is None or row["delisted_at"] is None:
        return None
    return str(row["delisted_at"])[:10]


def _bar_close(bar: dict[str, Any]) -> Decimal | None:
    return _d(bar["structured_fields"].get("close"))


def _bar_open(bar: dict[str, Any]) -> Decimal | None:
    return _d(bar["structured_fields"].get("open"))


def _bars_since(db: Any, security_id: str, entry_date: str, as_of: str) -> list[dict[str, Any]]:
    """Canonical bars strictly after entry_date, visible at as_of (for exits).

    The exit side uses the SAME realized-price bound as entry
    (_OUTCOME_VISIBILITY_BOUND) so labels are deterministic when replayed
    against a frozen snapshot -- realized prices, never decision-time data.
    """
    records = _visible_records(db, security_id, as_of)
    cutoff = as_of[:10]
    bars = [
        r
        for r in records
        if r["structured_fields"].get("record_type") == "price_bar"
        and str(r["structured_fields"].get("session_date", r["event_time"]))[:10] > entry_date
        and str(r["structured_fields"].get("session_date", r["event_time"]))[:10] <= cutoff
    ]
    bars.sort(key=lambda r: str(r["structured_fields"].get("session_date", r["event_time"]))[:10])
    # collapse duplicate sessions (identical bars collapse; conflicting -> drop)
    canonical: list[dict[str, Any]] = []
    index = 0
    while index < len(bars):
        session = str(
            bars[index]["structured_fields"].get("session_date", bars[index]["event_time"])
        )[:10]
        group = []
        while (
            index < len(bars)
            and str(
                bars[index]["structured_fields"].get("session_date", bars[index]["event_time"])
            )[:10]
            == session
        ):
            group.append(bars[index])
            index += 1
        serialized = {json.dumps(r["structured_fields"], sort_keys=True) for r in group}
        if len(serialized) == 1:
            canonical.append(group[0])
    return canonical


def build_outcome_label(
    research_db: ResearchDB,
    experiment_db: ResearchDB,
    *,
    dataset_snapshot_id: str,
    security_id: str,
    observation_date: str,
    horizon_sessions: int,
    benchmark_id: str | None = None,
    benchmark_daily_returns: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build ONE outcome label for one (security, observation_date, horizon).

    Reads research.db read-only; appends to experiment.db outcome_label.
    A delisted/unresolved outcome is NEVER dropped -- it is recorded with an
    explicit outcome_status and retained for coverage/censoring statistics.
    """
    if horizon_sessions not in HORIZON_SESSIONS:
        raise ValueError(f"horizon_sessions must be one of {HORIZON_SESSIONS}")
    with research_db.connect(read_only=True) as db:
        entry_bar, entry_session = next_session_on_or_after(db, security_id, observation_date)
        if entry_bar is None or entry_session is None:
            label = _base_label(
                dataset_snapshot_id, security_id, observation_date, horizon_sessions
            )
            label["outcome_status"] = "ENTRY_UNAVAILABLE"
            _insert_label(experiment_db, label)
            return label

        entry_close = _bar_close(entry_bar)
        entry_open = _bar_open(entry_bar)
        entry_price = entry_open if entry_open is not None and entry_open > 0 else entry_close
        entry_convention = (
            "next_session_open"
            if entry_open is not None and entry_open > 0
            else "next_session_close_fallback"
        )
        if entry_price is None or entry_price <= 0:
            label = _base_label(
                dataset_snapshot_id, security_id, observation_date, horizon_sessions
            )
            label["outcome_status"] = "ENTRY_UNAVAILABLE"
            _insert_label(experiment_db, label)
            return label

        # Exit = horizon sessions after entry, at close.
        bars = _bars_since(db, security_id, entry_session, _snapshot_end_asof())
        exit_bar: dict[str, Any] | None = None
        if len(bars) >= horizon_sessions:
            exit_bar = bars[horizon_sessions - 1]
            exit_close = _bar_close(exit_bar)
            if exit_close is None or exit_close <= 0:
                exit_close = None
            exit_session = str(
                exit_bar["structured_fields"].get("session_date", exit_bar["event_time"])
            )[:10]
        else:
            exit_close = None
            exit_session = None

        # Delisting visibility (realized side: same far-future bound as
        # entry/exit prices, so a mid-horizon delisting is classified
        # correctly; the security table's delisted_at is an additional
        # realized signal).
        delisting_event_ref, _delisted_date = _delisting_info(db, security_id, _snapshot_end_asof())
        security_delisted_at = _security_delisted_at(db, security_id)

        actions = _action_records(_visible_records(db, security_id, _snapshot_end_asof()))
        raw_return: Decimal | None = None
        total_return: Decimal | None = None
        if exit_close is not None and exit_close > 0:
            raw_return = exit_close / entry_price - 1
            adjustments = _cumulative_adjustments(actions, entry_session, exit_session)
            if adjustments != (None, None):
                cum_factor, cum_dividend = adjustments
                total_return = (exit_close * cum_factor + cum_dividend) / entry_price - 1

        label = _base_label(dataset_snapshot_id, security_id, observation_date, horizon_sessions)
        label["entry_convention"] = entry_convention
        label["entry_session_date"] = entry_session
        label["entry_price_evidence_ref"] = str(entry_bar["evidence_id"])
        label["exit_session_date"] = exit_session
        label["exit_price_evidence_ref"] = (
            str(exit_bar["evidence_id"]) if exit_bar is not None else None
        )
        label["raw_return"] = _quantize(raw_return) if raw_return is not None else None
        label["total_return"] = _quantize(total_return) if total_return is not None else None

        if security_delisted_at is not None and (
            exit_session is None or security_delisted_at <= exit_session
        ):
            label["outcome_status"] = "DELISTING_OUTCOME_UNKNOWN"
            label["delisting_event_ref"] = delisting_event_ref or "security.delisted_at"
        elif delisting_event_ref is not None and (
            exit_session is None or _delisted_date <= exit_session
        ):
            label["outcome_status"] = "DELISTING_OUTCOME_UNKNOWN"
            label["delisting_event_ref"] = delisting_event_ref
        elif exit_close is None:
            label["outcome_status"] = "CENSORED_INSUFFICIENT_HORIZON"
        else:
            label["outcome_status"] = "OBSERVED"

        if benchmark_id is not None and benchmark_daily_returns is not None:
            benchmark_return = _benchmark_return(
                benchmark_daily_returns, entry_session, exit_session
            )
            label["benchmark_id"] = benchmark_id
            label["benchmark_return"] = benchmark_return
            if benchmark_return is not None and total_return is not None:
                label["benchmark_relative_return"] = _quantize(
                    total_return - Decimal(str(benchmark_return))
                )

        _insert_label(experiment_db, label)
        return label


def _snapshot_end_asof() -> str:
    """Realized-price visibility bound for exit bars.

    Deliberately NOT utc_now(): outcome labels must be deterministic when
    replayed against a frozen snapshot. A far-future bound means "every bar
    in this snapshot" -- realized prices are outcome-side data and are not
    subject to decision-time PIT filtering (the feature path is, and is
    guarded by the lookahead canaries)."""
    from tradehub_research.portfolio.prices import _OUTCOME_VISIBILITY_BOUND

    return _OUTCOME_VISIBILITY_BOUND


def _benchmark_return(
    benchmark_daily_returns: dict[str, float], entry_session: str | None, exit_session: str | None
) -> float | None:
    if entry_session is None or exit_session is None:
        return None
    if entry_session >= exit_session:
        return None
    product = 1.0
    found = 0
    for session in sorted(benchmark_daily_returns):
        if entry_session < session <= exit_session:
            product *= 1.0 + benchmark_daily_returns[session]
            found += 1
    if found == 0:
        return None
    return product - 1.0


def _base_label(
    dataset_snapshot_id: str, security_id: str, observation_date: str, horizon_sessions: int
) -> dict[str, Any]:
    return {
        "label_id": str(uuid.uuid4()),
        "dataset_snapshot_id": dataset_snapshot_id,
        "security_id": security_id,
        "observation_date": observation_date,
        "horizon_sessions": horizon_sessions,
        "entry_convention": "next_session_close_fallback",
        "entry_session_date": None,
        "entry_price_evidence_ref": None,
        "exit_session_date": None,
        "exit_price_evidence_ref": None,
        "raw_return": None,
        "total_return": None,
        "benchmark_id": None,
        "benchmark_return": None,
        "benchmark_relative_return": None,
        "outcome_status": "OBSERVED",
        "delisting_event_ref": None,
        "builder_version": BUILDER_VERSION,
        "computed_at": utc_now(),
    }


def _insert_label(experiment_db: ResearchDB, label: dict[str, Any]) -> None:
    identity_material = json.dumps(
        {
            "dataset_snapshot_id": label["dataset_snapshot_id"],
            "security_id": label["security_id"],
            "observation_date": label["observation_date"],
            "horizon_sessions": label["horizon_sessions"],
            "builder_version": label["builder_version"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    label["label_id"] = hashlib.sha256(identity_material.encode()).hexdigest()
    with experiment_db.connect() as conn:
        try:
            conn.execute(
                "INSERT INTO outcome_label VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    label["label_id"],
                    label["dataset_snapshot_id"],
                    label["security_id"],
                    label["observation_date"],
                    label["horizon_sessions"],
                    label["entry_convention"],
                    label["entry_session_date"],
                    label["entry_price_evidence_ref"],
                    label["exit_session_date"],
                    label["exit_price_evidence_ref"],
                    label["raw_return"],
                    label["total_return"],
                    label["benchmark_id"],
                    label["benchmark_return"],
                    label["benchmark_relative_return"],
                    label["outcome_status"],
                    label["delisting_event_ref"],
                    label["builder_version"],
                    label["computed_at"],
                ),
            )
        except Exception:
            # idempotent: identical (security, observation_date, horizon,
            # snapshot, builder_version) rows are a no-op, not an error
            pass


def build_outcome_labels_for_observation(
    research_db: ResearchDB,
    experiment_db: ResearchDB,
    *,
    dataset_snapshot_id: str,
    security_id: str,
    observation_date: str,
    benchmark_id: str | None = None,
    benchmark_daily_returns: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Build all four horizon labels for one observation row."""
    results = []
    for horizon in HORIZON_SESSIONS:
        results.append(
            build_outcome_label(
                research_db,
                experiment_db,
                dataset_snapshot_id=dataset_snapshot_id,
                security_id=security_id,
                observation_date=observation_date,
                horizon_sessions=horizon,
                benchmark_id=benchmark_id,
                benchmark_daily_returns=benchmark_daily_returns,
            )
        )
    return results

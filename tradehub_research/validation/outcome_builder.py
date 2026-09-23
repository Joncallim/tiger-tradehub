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
from tradehub_research.validation import outcome_prices
from tradehub_research.validation.horizons import HORIZON_SESSIONS, required_exit_session

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
        # Shared outcome pricing: entry-session selection, entry price convention
        # and the return definition are identical to the live forward ledger.
        entry_bar, entry_session, entry_price, entry_convention = outcome_prices.entry_for(
            db, security_id, observation_date, visibility_bound=_snapshot_end_asof()
        )
        if entry_bar is None or entry_session is None or entry_price is None:
            label = _base_label(
                dataset_snapshot_id, security_id, observation_date, horizon_sessions
            )
            label["outcome_status"] = "ENTRY_UNAVAILABLE"
            _insert_label(experiment_db, label)
            return label

        # Exit = horizon sessions after entry, at close. Bars come from the shared
        # visible/canonical evidence layer, restricted to real market sessions: a
        # weekend/holiday bar (e.g. METRY's calendar-daily feed) is not a session
        # and must never set the exit session or the session count.
        bars = outcome_prices.visible_session_bars(
            db, security_id, after_session=entry_session, visibility_bound=_snapshot_end_asof()
        )
        exit_bar: dict[str, Any] | None = None
        # The shared exit rule: the horizon-th session after entry, or None while
        # the horizon is immature (never the latest available bar).
        # The exit is the CALENDAR's required session, exactly -- never "the N-th
        # available bar" (that equates N bars with N market sessions, so one absent
        # session would move the exit and the measured horizon). A frozen snapshot
        # has a final dataset boundary, so a missing required session is terminal
        # CENSORED; the live ledger keeps the same case pending instead.
        required_exit = required_exit_session(entry_session, horizon_sessions)
        exit_bar = outcome_prices.exit_bar_for(bars, required_exit)
        if exit_bar is not None:
            exit_close = outcome_prices.bar_close(exit_bar)
            if exit_close is None or exit_close <= 0:
                exit_close = None
            exit_session = required_exit
        else:
            exit_close = None
            exit_session = None

        # Delisting visibility (realized side: same far-future bound as
        # entry/exit prices, so a mid-horizon delisting is classified
        # correctly; the security table's delisted_at is an additional
        # realized signal).
        delisting_event_ref, _delisted_date = _delisting_info(db, security_id, _snapshot_end_asof())
        security_delisted_at = _security_delisted_at(db, security_id)

        raw_return: Decimal | None = None
        total_return: Decimal | None = None
        if exit_close is not None and exit_close > 0:
            raw_return, total_return = outcome_prices.outcome_returns(
                db,
                security_id,
                entry_session=entry_session,
                entry_price=entry_price,
                exit_session=exit_session,
                exit_close=exit_close,
                visibility_bound=_snapshot_end_asof(),
            )

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

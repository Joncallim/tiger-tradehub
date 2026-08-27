"""PIT-correct price/action resolution and deterministic risk measures.

Sources: the evidence ledger (``evidence_event`` rows with ``record_type`` in
``price_bar``/``split``/``dividend``), filtered to records visible at the
decision ``as_of`` with supersession chains resolved.  All arithmetic is
Decimal (precision 38, ROUND_HALF_UP); binary float never reaches a measure.

Canonical PIT rules (mirrors ``EvidenceStore.historical``):
- a record is visible only when its ``public_available_time <= as_of`` AND its
  ``pat_provenance`` is approved (``source_reported`` / ``derived_from_index``);
- the visible TERMINAL successor of a chain wins; if that terminal successor is
  withdrawn, the whole chain resolves to NO record (a withdrawal never
  resurrects the superseded record);
- price bars additionally require ``session_date <= as_of`` date (a bar with a
  future session date is never consumed for a past decision);
- at most ONE canonical bar per security/session: identical duplicates collapse;
  conflicting same-session bars make that session UNKNOWN;
- corporate actions use one normalized effective date (``effective_date`` else
  ``event_time``) everywhere, including ambiguity detection.

Corporate-action convention (provisional, versioned): return over a window is
``(close_t * cum_factor + cum_dividend) / close_prev - 1`` where
``cum_factor`` is the product of split factors with effective date in the
window and ``cum_dividend`` is the sum of per-share cash dividends multiplied
by the split factors effective at-or-before each dividend's effective date.
An ambiguous adjustment chain (e.g. same-day split+dividend) yields UNKNOWN.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tradehub_research.portfolio.types import INT64_MAX, INT64_MIN

DECIMAL_ZERO = Decimal(0)
PRECISION = 38
QUANTUM = Decimal("1e-12")

APPROVED_PAT_PROVENANCE = ("source_reported", "derived_from_index")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _q(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def _sqrt(value: Decimal) -> Decimal:
    return value.sqrt()


@dataclass(frozen=True)
class ReturnSeries:
    """A date-aligned series of daily total returns (Decimal, quantized)."""

    dates: tuple[str, ...]
    returns: tuple[Decimal, ...]
    evidence_ids: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.returns)

    def tail(self, window: int) -> ReturnSeries:
        return ReturnSeries(
            self.dates[-window:], self.returns[-window:], self.evidence_ids[-window:]
        )


def _session_key(record: dict[str, Any]) -> str:
    """Normalized UTC date key for a record (YYYY-MM-DD)."""
    return str(record["structured_fields"].get("session_date", record["event_time"]))[:10]


def _action_date(record: dict[str, Any]) -> str:
    """One normalized effective date for corporate actions."""
    return str(
        record["structured_fields"].get(
            "effective_date", record["structured_fields"].get("session_date", record["event_time"])
        )
    )[:10]


def _visible_records(db: Any, security_id: str, as_of: str) -> list[dict[str, Any]]:
    """Load evidence rows for a security and resolve supersession at as_of.

    Returns the effective (visible, non-withdrawn, approved-provenance)
    record per supersession chain.  Bounded: one query.
    """
    rows = db.execute(
        "SELECT evidence_id,supersedes_evidence_id,withdrawn,event_time,"
        "public_available_time,structured_fields,source_id,pat_provenance "
        "FROM evidence_event WHERE security_id=? ORDER BY event_time,evidence_id",
        (security_id,),
    ).fetchall()
    by_id: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    for row in rows:
        record = dict(row)
        record["structured_fields"] = json.loads(record["structured_fields"])
        by_id[record["evidence_id"]] = record
        predecessor = record["supersedes_evidence_id"]
        if predecessor:
            children.setdefault(predecessor, []).append(record["evidence_id"])
    visible: list[dict[str, Any]] = []
    for record in by_id.values():
        if record["supersedes_evidence_id"]:
            continue  # only chain roots are candidates
        current: dict[str, Any] | None = record
        chain: list[dict[str, Any]] = []
        while current is not None:
            chain.append(current)
            successors = children.get(current["evidence_id"], [])
            if len(successors) > 1:
                # ambiguous supersession graph: refuse to pick a winner
                current = None
                chain = []
                break
            current = by_id[successors[0]] if successors else None
        if not chain:
            continue
        # the latest VISIBLE chain member is the terminal; if it is withdrawn
        # the chain resolves to NO record (a withdrawal never resurrects the
        # superseded predecessor)
        visible_terminal: dict[str, Any] | None = None
        for item in chain:
            pat = item["public_available_time"]
            if pat is not None and pat <= as_of:
                visible_terminal = item
        if visible_terminal is None or visible_terminal["withdrawn"]:
            continue
        if visible_terminal["pat_provenance"] not in APPROVED_PAT_PROVENANCE:
            continue
        visible.append(visible_terminal)
    return visible


def _bar_records(records: list[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """Canonical bars: session cutoff, one bar per session (UNKNOWN on conflict).

    Identical duplicate bars for one session collapse; distinct conflicting
    bars for one session make that session UNKNOWN (excluded).  Bars with a
    session date after ``as_of`` are never consumed.
    """
    cutoff_date = as_of[:10]
    bars = [
        r
        for r in records
        if r["structured_fields"].get("record_type") == "price_bar"
        and _session_key(r) <= cutoff_date
    ]
    bars.sort(key=lambda r: (_session_key(r), r["evidence_id"]))
    canonical: list[dict[str, Any]] = []
    index = 0
    while index < len(bars):
        session = _session_key(bars[index])
        group: list[dict[str, Any]] = []
        while index < len(bars) and _session_key(bars[index]) == session:
            group.append(bars[index])
            index += 1
        if len(group) == 1:
            canonical.append(group[0])
            continue
        # multiple records for one session: collapse byte-identical dups
        serialized = {json.dumps(r["structured_fields"], sort_keys=True) for r in group}
        if len(serialized) == 1:
            canonical.append(group[0])
        # conflicting same-session bars -> session is UNKNOWN, excluded
    return canonical


def _action_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r for r in records if r["structured_fields"].get("record_type") in ("split", "dividend")
    ]


def _cumulative_adjustments(
    actions: list[dict[str, Any]], window_start: str, window_end: str
) -> tuple[Decimal | None, Decimal | None]:
    """cum_factor/cum_dividend for the window (start-exclusive, end-inclusive).

    Returns (None, None) when the adjustment chain is ambiguous.
    """
    window_actions = [r for r in actions if window_start < _action_date(r) <= window_end[:10]]
    if not window_actions:
        return Decimal(1), Decimal(0)
    splits = [r for r in window_actions if r["structured_fields"].get("record_type") == "split"]
    dividends = [
        r for r in window_actions if r["structured_fields"].get("record_type") == "dividend"
    ]
    for split in splits:
        for dividend in dividends:
            if _action_date(split) == _action_date(dividend):
                return None, None  # ambiguous same-day split+dividend
    cum_factor = Decimal(1)
    split_dates: list[tuple[str, Decimal]] = []
    for split in sorted(splits, key=_action_date):
        factor = _d(split["structured_fields"].get("factor", 1))
        if factor <= 0:
            return None, None
        cum_factor *= factor
        split_dates.append((_action_date(split), factor))
    cum_dividend = Decimal(0)
    for dividend in dividends:
        cash = _d(dividend["structured_fields"].get("cash", 0))
        if cash < 0:
            return None, None
        effective = _action_date(dividend)
        multiplier = Decimal(1)
        for split_date, factor in split_dates:
            if split_date <= effective:
                multiplier *= factor
        cum_dividend += cash * multiplier
    return cum_factor, cum_dividend


def total_return_series(
    db: Any,
    security_id: str,
    as_of: str,
) -> ReturnSeries:
    """Daily total-return series from PIT-visible canonical bars and actions."""
    records = _visible_records(db, security_id, as_of)
    bars = _bar_records(records, as_of)
    actions = _action_records(records)
    dates: list[str] = []
    returns: list[Decimal] = []
    evidence_ids: list[str] = []
    previous: dict[str, Any] | None = None
    for bar in bars:
        fields = bar["structured_fields"]
        close = fields.get("close")
        session_date = _session_key(bar)
        if close is None or _d(close) <= 0:
            previous = None
            continue  # zero/invalid price breaks the return chain
        if previous is not None:
            previous_fields = previous["structured_fields"]
            previous_close = previous_fields.get("close")
            previous_session = _session_key(previous)
            if previous_close is not None and _d(previous_close) > 0:
                adjustments = _cumulative_adjustments(actions, previous_session, session_date)
                if adjustments == (None, None):
                    previous = None
                    continue  # ambiguous action chain: UNKNOWN, break the chain
                cum_factor, cum_dividend = adjustments
                numerator = _d(close) * cum_factor + cum_dividend
                denominator = _d(previous_close)
                if denominator == 0:
                    previous = None
                    continue
                daily_return = _q(numerator / denominator - Decimal(1))
                dates.append(session_date)
                returns.append(daily_return)
                # lineage: both endpoint bars plus every action in the window
                dependency_ids = [previous["evidence_id"], bar["evidence_id"]]
                for action in actions:
                    if previous_session < _action_date(action) <= session_date:
                        dependency_ids.append(action["evidence_id"])
                evidence_ids.append(",".join(sorted(set(dependency_ids))))
        previous = bar
    return ReturnSeries(tuple(dates), tuple(returns), tuple(evidence_ids))


def sample_volatility(series: ReturnSeries, window: int, min_observations: int) -> Decimal | None:
    """Daily sample volatility (Decimal, stdev); None when unusable."""
    if min_observations < 2:
        min_observations = 2  # a 1-observation variance is undefined
    tail = series.tail(window) if len(series) > window else series
    if len(tail) < min_observations:
        return None
    values = tail.returns
    n = len(values)
    mean = sum(values, DECIMAL_ZERO) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance == 0:
        return None  # zero variance is not a usable volatility signal
    return _q(variance.sqrt())


def annualize(daily_vol: Decimal, sessions: int) -> Decimal:
    return _q(daily_vol * Decimal(sessions).sqrt())


def paired_correlation(
    a: ReturnSeries, b: ReturnSeries, window: int, min_overlap: int
) -> Decimal | None:
    """Pearson correlation over date-aligned overlapping returns.

    Returns None when the paired sample is below ``min_overlap`` or has zero
    variance on either side.
    """
    by_date_a = dict(zip(a.dates, a.returns, strict=False))
    by_date_b = dict(zip(b.dates, b.returns, strict=False))
    pairs: list[tuple[Decimal, Decimal]] = []
    for date, value_a in by_date_a.items():
        value_b = by_date_b.get(date)
        if value_b is not None:
            pairs.append((value_a, value_b))
    pairs = pairs[-window:] if len(pairs) > window else pairs
    if len(pairs) < min_overlap:
        return None
    n = len(pairs)
    mean_a = sum(p[0] for p in pairs) / n
    mean_b = sum(p[1] for p in pairs) / n
    numerator = sum((p[0] - mean_a) * (p[1] - mean_b) for p in pairs)
    var_a = sum((p[0] - mean_a) ** 2 for p in pairs)
    var_b = sum((p[1] - mean_b) ** 2 for p in pairs)
    if var_a == 0 or var_b == 0:
        return None
    return _q(numerator / (var_a * var_b).sqrt())


def average_dollar_volume(
    db: Any, security_id: str, as_of: str, window: int, min_observations: int
) -> int | None:
    """Arithmetic mean of close*volume over the trailing window, in micro-USD.

    Counts distinct sessions (canonical bars), never duplicate rows.
    """
    records = _visible_records(db, security_id, as_of)
    bars = _bar_records(records, as_of)[-window:]
    if len(bars) < min_observations:
        return None
    total = Decimal(0)
    for bar in bars:
        fields = bar["structured_fields"]
        close = fields.get("close")
        volume = fields.get("volume")
        if close is None or volume is None or _d(close) <= 0 or _d(volume) < 0:
            return None
        total += _d(close) * _d(volume)
    mean = total / len(bars)
    micro = (mean * Decimal(1_000_000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if not INT64_MIN <= micro <= INT64_MAX:
        raise OverflowError("average dollar volume exceeds signed 64-bit range")
    return int(micro)


def latest_close_microusd(db: Any, security_id: str, as_of: str) -> tuple[int | None, str | None]:
    """Most recent PIT-visible canonical close (micro-USD) and its session date."""
    records = _visible_records(db, security_id, as_of)
    bars = _bar_records(records, as_of)
    if not bars:
        return None, None
    bar = bars[-1]
    close = bar["structured_fields"].get("close")
    if close is None or _d(close) <= 0:
        return None, None
    micro = (_d(close) * Decimal(1_000_000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(micro), _session_key(bar)


def next_session_on_or_after(
    db: Any, security_id: str, after_ts: str
) -> tuple[dict[str, Any] | None, str | None]:
    """First canonical bar with session date > ``after_ts``'s date.

    Returns (bar, session_date) or (None, None) when no eligible session
    exists. This is the outcome-builder ENTRY convention (handoff sec 6.3):
    "first eligible session after the observation timestamp". The entry
    price is the REALIZED next-session open/close -- an outcome-side label,
    deliberately NOT a decision-time-visible price (the decision-time
    feature path remains PIT-filtered and is guarded by the lookahead
    canaries). A far-future visibility bound is therefore correct here:
    realized labels may use realized prices; only features may not.
    """
    records = _visible_records(db, security_id, _OUTCOME_VISIBILITY_BOUND)
    bars = _bar_records(records, _OUTCOME_VISIBILITY_BOUND)
    cutoff = after_ts[:10]
    for bar in bars:
        if _session_key(bar) > cutoff:
            return bar, _session_key(bar)
    return None, None


_OUTCOME_VISIBILITY_BOUND = "9999-12-31T00:00:00Z"

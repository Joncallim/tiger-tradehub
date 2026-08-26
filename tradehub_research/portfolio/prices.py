"""PIT-correct price/action resolution and deterministic risk measures.

Sources: the evidence ledger (``evidence_event`` rows with ``record_type`` in
``price_bar``/``split``/``dividend``), filtered to records visible at the
decision ``as_of`` with supersession chains resolved.  All arithmetic is
Decimal (precision 38, ROUND_HALF_UP); binary float never reaches a measure.

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


def _visible_records(db: Any, security_id: str, as_of: str) -> list[dict[str, Any]]:
    """Load evidence rows for a security and resolve supersession at as_of.

    Returns the effective (non-withdrawn) record per supersession chain that
    is publicly available at or before ``as_of``.  Bounded: one query.
    """
    rows = db.execute(
        "SELECT evidence_id,supersedes_evidence_id,withdrawn,event_time,"
        "public_available_time,structured_fields,source_id "
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
        effective = None
        for item in chain:
            pat = item["public_available_time"]
            if pat is not None and pat <= as_of and not item["withdrawn"]:
                effective = item
        if effective is not None:
            visible.append(effective)
    return visible


def _bar_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bars = [r for r in records if r["structured_fields"].get("record_type") == "price_bar"]
    return sorted(bars, key=lambda r: r["structured_fields"].get("session_date", r["event_time"]))


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
    window_actions = [
        r
        for r in actions
        if window_start
        < r["structured_fields"].get("effective_date", r["event_time"])
        <= window_end
    ]
    if not window_actions:
        return Decimal(1), Decimal(0)
    same_day = [r for r in window_actions if r["structured_fields"].get("record_type") == "split"]
    same_day_dividends = [
        r for r in window_actions if r["structured_fields"].get("record_type") == "dividend"
    ]
    for split in same_day:
        for dividend in same_day_dividends:
            if split["structured_fields"].get("effective_date") == dividend[
                "structured_fields"
            ].get("effective_date"):
                return None, None  # ambiguous same-day split+dividend
    cum_factor = Decimal(1)
    splits: list[tuple[str, Decimal]] = []
    for split in sorted(
        (r for r in window_actions if r["structured_fields"].get("record_type") == "split"),
        key=lambda r: r["structured_fields"].get("effective_date", r["event_time"]),
    ):
        factor = _d(split["structured_fields"].get("factor", 1))
        if factor <= 0:
            return None, None
        cum_factor *= factor
        splits.append(
            (split["structured_fields"].get("effective_date", split["event_time"]), factor)
        )
    cum_dividend = Decimal(0)
    for dividend in window_actions:
        if dividend["structured_fields"].get("record_type") != "dividend":
            continue
        cash = _d(dividend["structured_fields"].get("cash", 0))
        if cash < 0:
            return None, None
        effective = dividend["structured_fields"].get("effective_date", dividend["event_time"])
        multiplier = Decimal(1)
        for split_date, factor in splits:
            if split_date <= effective:
                multiplier *= factor
        cum_dividend += cash * multiplier
    return cum_factor, cum_dividend


def total_return_series(
    db: Any,
    security_id: str,
    as_of: str,
) -> ReturnSeries:
    """Daily total-return series from PIT-visible bars and corporate actions."""
    records = _visible_records(db, security_id, as_of)
    bars = _bar_records(records)
    actions = _action_records(records)
    dates: list[str] = []
    returns: list[Decimal] = []
    evidence_ids: list[str] = []
    previous: dict[str, Any] | None = None
    for bar in bars:
        fields = bar["structured_fields"]
        close = fields.get("close")
        session_date = fields.get("session_date", bar["event_time"])
        if close is None or _d(close) <= 0:
            previous = None
            continue  # zero/invalid price breaks the return chain
        if previous is not None:
            previous_fields = previous["structured_fields"]
            previous_close = previous_fields.get("close")
            previous_session = previous_fields.get("session_date", previous["event_time"])
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
                evidence_ids.append(bar["evidence_id"])
        previous = bar
    return ReturnSeries(tuple(dates), tuple(returns), tuple(evidence_ids))


def sample_volatility(series: ReturnSeries, window: int, min_observations: int) -> Decimal | None:
    """Annualized sample volatility (Decimal, daily stdev * sqrt(sessions))."""
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
    """Arithmetic mean of close*volume over the trailing window, in micro-USD."""
    records = _visible_records(db, security_id, as_of)
    bars = _bar_records(records)[-window:]
    if len(bars) < min_observations:
        return None
    total = 0
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
    """Most recent PIT-visible close (micro-USD) and its session date."""
    records = _visible_records(db, security_id, as_of)
    bars = _bar_records(records)
    if not bars:
        return None, None
    bar = bars[-1]
    close = bar["structured_fields"].get("close")
    if close is None or _d(close) <= 0:
        return None, None
    micro = (_d(close) * Decimal(1_000_000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(micro), bar["structured_fields"].get("session_date", bar["event_time"])

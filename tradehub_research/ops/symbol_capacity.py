"""Rolling-month distinct-symbol capacity planning (Tiingo's 450-symbol ceiling).

The contract
------------
Tiingo counts **distinct symbols requested per rolling month**. The estate's
durable reservation set is ``bootstrap_symbol`` in
``<adapter_cache_dir>/tiingo-operational.sqlite``: at most ``limit`` (450) rows,
each carrying ``first_requested_at``, pruned once older than 30 days.

Before this module the ceiling was discovered the worst possible way -- by
issuing a request and letting the reservation raise
``RuntimeError("Tiingo 450-symbol rolling-month bootstrap ceiling reached")``
mid-run. At the time of writing the set held **444 of 450**, so the fleet was six
new symbols away from a capability failure that would have looked like a
provider outage.

What this module guarantees
---------------------------
* **Plan before spending.** ``plan_symbol_capacity`` computes used / headroom /
  admissible / deferred *read-only* (it never reserves and never spends quota),
  so a run knows its capacity position before its first fetch.
* **Revisiting is free.** A symbol already inside the rolling window consumes no
  new capacity. Existing (especially active or stale) symbols therefore stay
  refreshable even at 450/450 -- being at the ceiling must not stop the fleet
  from refreshing what it already owns.
* **Deterministic degradation.** When the requested set exceeds headroom, the
  planner admits what fits, in priority order, and returns the rest as
  ``deferred`` with a reason -- it never silently drops coverage and never
  churns reservations (admission order is stable, so a retry admits the same
  symbols rather than thrashing the set).
* **No surprise failures.** ``require_headroom`` raises a *typed*
  ``SymbolCapacityExceeded`` with the real numbers, so a capacity limit is
  classified and reported as capacity -- not as a provider/API fault.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Provider ceiling on distinct symbols per rolling month.
DEFAULT_SYMBOL_LIMIT = 450
ROLLING_WINDOW_SECONDS = 30 * 86400


class SymbolCapacityExceeded(RuntimeError):
    """Raised *before* a request when admitting a new symbol cannot fit.

    Distinct from a provider error on purpose: this is a known, planned
    constraint of the licence, not an outage.
    """

    def __init__(self, symbol: str, used: int, limit: int) -> None:
        self.symbol = symbol
        self.used = used
        self.limit = limit
        super().__init__(
            f"symbol capacity exhausted: cannot admit {symbol!r} "
            f"({used}/{limit} distinct symbols in the rolling month)"
        )


@dataclass
class CapacityPlan:
    limit: int
    used: int
    already_reserved: list[str] = field(default_factory=list)
    admissible_new: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    #: symbol -> why it could not be admitted
    reasons: dict[str, str] = field(default_factory=dict)
    #: epoch seconds when the oldest reservation expires (None when nothing is reserved)
    next_headroom_at: float | None = None

    @property
    def headroom(self) -> int:
        """New symbols that can still be admitted right now."""
        return max(0, self.limit - self.used)

    @property
    def revisitable(self) -> int:
        """Requested symbols that consume no new capacity."""
        return len(self.already_reserved)

    @property
    def at_capacity(self) -> bool:
        return self.headroom <= 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "headroom": self.headroom,
            "revisitable": self.revisitable,
            "admissible_new": len(self.admissible_new),
            "deferred": len(self.deferred),
            "at_capacity": self.at_capacity,
            "next_headroom_at": self.next_headroom_at,
            "deferred_reasons": self.reasons,
        }


def plan_symbol_capacity(
    quota: Any,
    requested: Iterable[str] | None = None,
    *,
    now: float,
    limit: int = DEFAULT_SYMBOL_LIMIT,
    active: Sequence[str] = (),
) -> CapacityPlan:
    """Plan which requested symbols fit inside the rolling-month ceiling.

    Read-only: uses ``quota.bootstrap_usage`` (introspection), never
    ``reserve_bootstrap_symbol``. Ordering rules, so the outcome is stable and
    prioritised rather than accidental:

      1. symbols in ``active`` (currently feeding signals) first
      2. then symbols already inside the rolling window (free to revisit)
      3. then new symbols in the caller's order

    ``requested=None`` means "plan against the current reserved set only".
    """
    usage = quota.bootstrap_usage(now, limit) if hasattr(quota, "bootstrap_usage") else {}
    reserved = {str(row["symbol"]).upper() for row in usage.get("symbols", [])}
    used = int(usage.get("used", len(reserved)))
    stamps = [float(row.get("first_requested_at") or 0.0) for row in usage.get("symbols", [])]
    next_headroom_at = (min(stamps) + ROLLING_WINDOW_SECONDS) if stamps else None

    plan = CapacityPlan(limit=limit, used=used, next_headroom_at=next_headroom_at)
    if requested is None:
        return plan

    wanted: list[str] = []
    seen: set[str] = set()
    for symbol in list(active) + list(requested):
        key = str(symbol).upper()
        if key and key not in seen:
            seen.add(key)
            wanted.append(key)

    known = [s for s in wanted if s in reserved]
    pending = [s for s in wanted if s not in reserved]
    plan.already_reserved = known

    room = plan.headroom
    plan.admissible_new = pending[:room]
    plan.deferred = pending[room:]
    for symbol in plan.deferred:
        plan.reasons[symbol] = (
            f"rolling-month symbol capacity full ({used}/{limit}); "
            f"admits {len(plan.admissible_new)} of {len(pending)} new symbols this run"
        )
    return plan


def require_headroom(
    quota: Any, symbol: str, *, now: float, limit: int = DEFAULT_SYMBOL_LIMIT
) -> None:
    """Fail closed *before* a request if admitting ``symbol`` cannot fit.

    A symbol already inside the window is always allowed -- the ceiling limits
    new distinct symbols, not refreshes of ones the fleet already owns.
    """
    plan = plan_symbol_capacity(quota, [symbol], now=now, limit=limit)
    if plan.deferred:
        raise SymbolCapacityExceeded(symbol, plan.used, limit)


def capacity_report(quota: Any, *, now: float, limit: int = DEFAULT_SYMBOL_LIMIT) -> dict[str, Any]:
    """Current capacity position, for the health report."""
    plan = plan_symbol_capacity(quota, None, now=now, limit=limit)
    return plan.as_dict()


__all__ = [
    "DEFAULT_SYMBOL_LIMIT",
    "ROLLING_WINDOW_SECONDS",
    "CapacityPlan",
    "SymbolCapacityExceeded",
    "capacity_report",
    "plan_symbol_capacity",
    "require_headroom",
]

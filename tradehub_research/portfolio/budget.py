"""Daily aggregate activity budget, derived from an immutable ledger.

Usage is COUNT/SUM over ``trade_proposal`` for the UTC activity date — never a
mutable counter.  The ``portfolio_activity_day`` row binds the date to the
FIRST run's policy, caps, AND starting cash (first writer wins); enforcement
reads the STORED values, so a policy-version change cannot reset the day's
allowance and a later run cannot spend the same cash twice.  Drafts are
admitted whole (never solver-clipped) in deterministic priority order, with a
running cash accumulator: remaining cash = day_start_cash - SUM(admitted BUY
notional).  PAPER SELL proceeds are NEVER credited — an unexecuted paper
recommendation must not fund a buy.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.portfolio.policy import PolicySpec
from tradehub_research.portfolio.types import Action, C


@contextmanager
def _nullcontext(value: Any):
    yield value


@dataclass(frozen=True)
class BudgetState:
    activity_date: str
    policy_version: str
    max_actionable_count: int
    max_notional_microusd: int
    used_count: int
    used_notional_microusd: int
    day_start_cash_microusd: int | None = None
    used_buy_notional_microusd: int = 0

    def admit(self, draft_notional_microusd: int) -> bool:
        return (
            self.used_count + 1 <= self.max_actionable_count
            and self.used_notional_microusd + draft_notional_microusd <= self.max_notional_microusd
        )

    @property
    def remaining_cash_microusd(self) -> int | None:
        if self.day_start_cash_microusd is None:
            return None
        return self.day_start_cash_microusd - self.used_buy_notional_microusd


class Budget:
    def __init__(self, database: ResearchDB):
        self.database = database

    def bind_day(
        self,
        activity_date: str,
        policy: PolicySpec,
        *,
        created_at: str | None = None,
        db: Any | None = None,
        day_start_cash_microusd: int | None = None,
    ) -> BudgetState:
        """Bind the day to the first run's policy/caps/cash; enforce stored values.

        Raises ValueError when an existing day row was bound to a different
        policy version (fail closed — never silently switch the allowance).
        ``db`` injects an open transaction connection when the caller already
        holds one (engine runs); otherwise a private connection is used.
        """
        created_at = created_at or utc_now()
        context = self.database.connect() if db is None else _nullcontext(db)
        with context as db:
            existing = db.execute(
                "SELECT policy_version,max_actionable_count,max_notional_microusd,"
                "day_start_cash_microusd "
                "FROM portfolio_activity_day WHERE activity_date=?",
                (activity_date,),
            ).fetchone()
            if existing is not None:
                if existing["policy_version"] != policy.policy_version:
                    raise ValueError(
                        f"activity date {activity_date} already bound to policy "
                        f"{existing['policy_version']}, run policy is {policy.policy_version}"
                    )
                caps = (
                    int(existing["max_actionable_count"]),
                    int(existing["max_notional_microusd"]),
                )
                stored_cash = existing["day_start_cash_microusd"]
                if stored_cash is not None and day_start_cash_microusd is not None:
                    if int(stored_cash) != day_start_cash_microusd:
                        raise ValueError(
                            f"activity date {activity_date} already bound to day_start_cash "
                            f"{stored_cash}, run snapshot cash is {day_start_cash_microusd}"
                        )
                day_cash = int(stored_cash) if stored_cash is not None else day_start_cash_microusd
            else:
                caps = (
                    int(policy.budget["max_actionable_count"]),
                    int(policy.budget["max_notional_microusd"]),
                )
                day_cash = day_start_cash_microusd
                material = {
                    "activity_date": activity_date,
                    "policy_version": policy.policy_version,
                    "max_actionable_count": caps[0],
                    "max_notional_microusd": caps[1],
                    "day_start_cash_microusd": day_cash,
                }
                db.execute(
                    "INSERT INTO portfolio_activity_day("
                    "activity_date,policy_version,max_actionable_count,max_notional_microusd,"
                    "day_start_cash_microusd,input_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        activity_date,
                        policy.policy_version,
                        caps[0],
                        caps[1],
                        day_cash,
                        C(material),
                        created_at,
                    ),
                )
            usage = db.execute(
                "SELECT count(*) AS used_count,coalesce(sum(max_notional_microusd),0) "
                "AS used_notional,coalesce(sum(CASE WHEN action='BUY' THEN "
                "max_notional_microusd ELSE 0 END),0) AS used_buy_notional "
                "FROM trade_proposal WHERE activity_date=?",
                (activity_date,),
            ).fetchone()
        return BudgetState(
            activity_date=activity_date,
            policy_version=policy.policy_version,
            max_actionable_count=caps[0],
            max_notional_microusd=caps[1],
            used_count=int(usage["used_count"]),
            used_notional_microusd=int(usage["used_notional"]),
            day_start_cash_microusd=day_cash,
            used_buy_notional_microusd=int(usage["used_buy_notional"]),
        )

    @staticmethod
    def draft_sort_key(
        policy: PolicySpec,
        draft: dict[str, Any],
    ) -> tuple[int, int, str]:
        """Deterministic admission order: category, reason, security_id."""
        category_priority = policy.budget["category_priority"]
        reason_priority = policy.budget["reason_priority"]
        category = str(draft.get("category", "score_band"))
        raw_reasons = draft.get("reason_codes", draft.get("reason_codes_json", []))
        if isinstance(raw_reasons, str):
            try:
                reasons = json.loads(raw_reasons)
            except (TypeError, ValueError):
                reasons = []
        else:
            reasons = list(raw_reasons)
        category_index = (
            category_priority.index(category)
            if category in category_priority
            else len(category_priority)
        )
        reason = reasons[0] if reasons else ""
        reason_index = (
            reason_priority.index(reason) if reason in reason_priority else len(reason_priority)
        )
        return (category_index, reason_index, str(draft.get("security_id", "")))


def admit_drafts(
    budget: BudgetState,
    drafts: list[dict[str, Any]],
    policy: PolicySpec,
    *,
    starting_cash_microusd: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Admit drafts whole in deterministic order; returns (admitted, rejected).

    Cash enforcement uses the DAY-BOUND starting cash (first writer wins),
    never the per-run snapshot: remaining = day_start_cash - SUM(admitted BUY
    notional).  ``starting_cash_microusd`` is retained for API compatibility
    but only applies when the day row carries no bound cash (SELL-only days).
    PAPER SELL proceeds are never credited to the accumulator.
    """
    ordered = sorted(drafts, key=lambda draft: Budget.draft_sort_key(policy, draft))
    admitted: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    remaining = budget
    cash_remaining = budget.remaining_cash_microusd
    if cash_remaining is None:
        cash_remaining = starting_cash_microusd
    for draft in ordered:
        notional = int(draft["max_notional_microusd"])
        action = draft.get("action")
        if action == Action.BUY.value and cash_remaining is not None:
            if notional > cash_remaining:
                rejected[draft["security_id"]] = "cash_insufficient"
                continue
        if not remaining.admit(notional):
            rejected[draft["security_id"]] = "daily_budget_exhausted"
            continue
        admitted.append(draft)
        remaining = BudgetState(
            activity_date=budget.activity_date,
            policy_version=budget.policy_version,
            max_actionable_count=budget.max_actionable_count,
            max_notional_microusd=budget.max_notional_microusd,
            used_count=remaining.used_count + 1,
            used_notional_microusd=remaining.used_notional_microusd + notional,
            day_start_cash_microusd=budget.day_start_cash_microusd,
            used_buy_notional_microusd=(
                remaining.used_buy_notional_microusd + notional
                if action == Action.BUY.value
                else remaining.used_buy_notional_microusd
            ),
        )
        if cash_remaining is not None and action == Action.BUY.value:
            cash_remaining -= notional
    return admitted, rejected

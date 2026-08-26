"""Daily aggregate activity budget, derived from an immutable ledger.

Usage is COUNT/SUM over ``trade_proposal`` for the UTC activity date — never a
mutable counter.  The ``portfolio_activity_day`` row binds the date to the
FIRST run's policy and caps (first writer wins); enforcement reads the STORED
caps, so a policy-version change cannot reset the day's allowance.  Drafts are
admitted whole (never solver-clipped) in deterministic priority order, with a
running cash accumulator so multiple BUYs cannot collectively exceed cash.
"""

from __future__ import annotations

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

    def admit(self, draft_notional_microusd: int) -> bool:
        return (
            self.used_count + 1 <= self.max_actionable_count
            and self.used_notional_microusd + draft_notional_microusd <= self.max_notional_microusd
        )


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
    ) -> BudgetState:
        """Bind the day to the first run's policy/caps; enforce stored caps.

        Raises ValueError when an existing day row was bound to a different
        policy version (fail closed — never silently switch the allowance).
        ``db`` injects an open transaction connection when the caller already
        holds one (engine runs); otherwise a private connection is used.
        """
        created_at = created_at or utc_now()
        context = self.database.connect() if db is None else _nullcontext(db)
        with context as db:
            existing = db.execute(
                "SELECT policy_version,max_actionable_count,max_notional_microusd "
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
            else:
                caps = (
                    int(policy.budget["max_actionable_count"]),
                    int(policy.budget["max_notional_microusd"]),
                )
                material = {
                    "activity_date": activity_date,
                    "policy_version": policy.policy_version,
                    "max_actionable_count": caps[0],
                    "max_notional_microusd": caps[1],
                }
                db.execute(
                    "INSERT INTO portfolio_activity_day("
                    "activity_date,policy_version,max_actionable_count,max_notional_microusd,"
                    "input_hash,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        activity_date,
                        policy.policy_version,
                        caps[0],
                        caps[1],
                        C(material),
                        created_at,
                    ),
                )
            usage = db.execute(
                "SELECT count(*) AS used_count,coalesce(sum(max_notional_microusd),0) "
                "AS used_notional FROM trade_proposal WHERE activity_date=?",
                (activity_date,),
            ).fetchone()
        return BudgetState(
            activity_date=activity_date,
            policy_version=policy.policy_version,
            max_actionable_count=caps[0],
            max_notional_microusd=caps[1],
            used_count=int(usage["used_count"]),
            used_notional_microusd=int(usage["used_notional"]),
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
        reasons = list(draft.get("reason_codes", []))
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
    starting_cash_microusd: int | None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Admit drafts whole in deterministic order; returns (admitted, rejected).

    ``starting_cash_microusd`` None disables the cash accumulator (unknown
    cash can only occur for SELL-only contexts; BUY drafts with unknown cash
    are blocked earlier by risk).
    """
    ordered = sorted(drafts, key=lambda draft: Budget.draft_sort_key(policy, draft))
    admitted: list[dict[str, Any]] = []
    rejected: dict[str, str] = {}
    remaining = budget
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
        )
        if cash_remaining is not None:
            if action == Action.BUY.value:
                cash_remaining -= notional
            elif action == Action.SELL.value:
                cash_remaining += notional
    return admitted, rejected

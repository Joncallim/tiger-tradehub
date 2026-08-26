"""Fixed-field eligibility rule evaluation and thesis-break gating.

Eligibility rules are pure field matchers over deterministic inputs; they can
never contain expressions and never accept a state/action/reason from a model.
Signal eligibility, persistence, and risk are separate results composed by the
engine; score alone cannot authorize anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tradehub_research.portfolio.policy import PolicySpec
from tradehub_research.portfolio.types import (
    PositionRequirement,
    State,
    TriggerKind,
    VerificationStatus,
)


def _rule_matches(rule: dict[str, Any], context: dict[str, Any]) -> bool:
    """Field-level match of one rule against a decision context.

    ``context`` keys: conviction_ppm, data_quality_ppm, agreement_ppm,
    trajectory, opportunity_ppm (int|None), opportunity_known (bool),
    position_present (bool), sector_coverage_status (str).
    Returns False when any field fails; missing opportunity that a rule
    requires is surfaced by the caller via ``opportunity_blocked``.
    """
    conviction = context.get("conviction_ppm")
    data_quality = context.get("data_quality_ppm")
    agreement = context.get("agreement_ppm")
    trajectory = context.get("trajectory")
    position_present = bool(context.get("position_present"))
    coverage = context.get("sector_coverage_status")

    if conviction is None:
        return False
    conviction_min = rule.get("conviction_min_ppm", 0)
    conviction_max = rule.get("conviction_max_ppm", 1_000_000)
    if not conviction_min <= conviction <= conviction_max:
        return False
    quality_min = rule.get("data_quality_min_ppm", 0)
    if data_quality is None or data_quality < quality_min:
        return False
    agreement_min = rule.get("agreement_min_ppm", 0)
    if agreement is None or agreement < agreement_min:
        return False
    trajectories = rule.get("trajectories", [])
    if trajectories and trajectory not in trajectories:
        return False
    position_requirement = rule.get("position")
    if position_requirement == PositionRequirement.ABSENT.value and position_present:
        return False
    if position_requirement == PositionRequirement.PRESENT.value and not position_present:
        return False
    opportunity_min = rule.get("opportunity_min_ppm")
    opportunity_max = rule.get("opportunity_max_ppm")
    if opportunity_min is not None or opportunity_max is not None:
        opportunity = context.get("opportunity_ppm")
        opportunity_known = bool(context.get("opportunity_known"))
        if not opportunity_known:
            return False  # required opportunity is UNKNOWN: rule cannot pass
        if opportunity_min is not None and opportunity < opportunity_min:
            return False
        if opportunity_max is not None and opportunity > opportunity_max:
            return False
    coverage_statuses = rule.get("allowed_sector_coverage_statuses")
    if coverage_statuses and coverage not in coverage_statuses:
        return False
    return True


def opportunity_blocks(rule: dict[str, Any], context: dict[str, Any]) -> bool:
    """True when a rule needs opportunity but the opportunity input is UNKNOWN."""
    opportunity_min = rule.get("opportunity_min_ppm")
    opportunity_max = rule.get("opportunity_max_ppm")
    if opportunity_min is None and opportunity_max is None:
        return False
    return not bool(context.get("opportunity_known"))


@dataclass
class EligibilityResult:
    rule_id: str | None
    from_state: State | None
    to_state: State | None
    trigger_kind: str | None
    status: str  # PASS | INELIGIBLE | UNKNOWN | BLOCKED
    opportunity_blocked: bool = False
    reason_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "from_state": self.from_state.value if self.from_state else None,
            "to_state": self.to_state.value if self.to_state else None,
            "trigger_kind": self.trigger_kind,
            "status": self.status,
            "opportunity_blocked": self.opportunity_blocked,
            "reason_code": self.reason_code,
        }


def evaluate_eligibility(
    policy: PolicySpec,
    current_state: State,
    context: dict[str, Any],
) -> EligibilityResult:
    """Evaluate all rules for the current state; return the highest-priority match.

    Status semantics:
      PASS        — a rule matched (transition still gated by persistence/risk).
      INELIGIBLE  — rules evaluated; none matched.
      UNKNOWN     — blocking input missing (score absent, or a matching rule
                    needs opportunity that is UNKNOWN).
      BLOCKED     — policy coverage failure (sector coverage not allowed by any
                    rule) — deterministic, not missing-data.
    """
    candidates = [
        rule for rule in policy.eligibility_rules if rule["from_state"] == current_state.value
    ]
    if not candidates:
        # No outgoing rule for this state: deterministic ineligibility.
        return EligibilityResult(None, current_state, None, None, "INELIGIBLE")
    if context.get("conviction_ppm") is None and context.get("trigger_override") is None:
        # A scored decision with no score snapshot cannot match SCORE_BAND rules.
        if any(rule.get("trigger_kind") == TriggerKind.SCORE_BAND.value for rule in candidates):
            return EligibilityResult(None, current_state, None, None, "UNKNOWN")
    for rule in sorted(candidates, key=lambda r: r["priority"]):
        trigger = rule["trigger_kind"]
        if trigger == TriggerKind.VERIFIED_THESIS_BREAK.value:
            if not context.get("verified_break_eligible"):
                continue
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "PASS",
                reason_code="thesis_broken",
            )
        if trigger == TriggerKind.DATA_INTEGRITY.value:
            if not context.get("data_integrity_failure"):
                continue
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "PASS",
                reason_code="data_integrity",
            )
        if trigger == TriggerKind.POLICY_INELIGIBLE.value:
            if not context.get("policy_ineligible"):
                continue
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "PASS",
                reason_code="policy_ineligible",
            )
        if trigger == TriggerKind.RISK_REDUCTION.value:
            if not context.get("risk_reduction_trigger"):
                continue
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "PASS",
                reason_code="risk_reduction",
            )
        if trigger == TriggerKind.THESIS_REALISED.value:
            if not context.get("thesis_realised"):
                continue
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "PASS",
                reason_code="thesis_realised",
            )
        # SCORE_BAND: fixed-field match
        if opportunity_blocks(rule, context):
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "UNKNOWN",
                opportunity_blocked=True,
            )
        if _rule_matches(rule, context):
            return EligibilityResult(
                rule["rule_id"],
                current_state,
                State(rule["to_state"]),
                trigger,
                "PASS",
            )
    return EligibilityResult(None, current_state, None, None, "INELIGIBLE")


def latest_verified_break(
    db: Any,
    security_id: str,
    as_of: str,
    allowed_methods: list[str],
    max_age_calendar_days: int,
) -> dict[str, Any] | None:
    """Latest structured VERIFIED thesis break usable at ``as_of``.

    Only rows with status VERIFIED, an allowed verification method, and
    ``verified_at <= as_of`` within ``max_age_calendar_days`` qualify.
    Condition text is never read by the engine.
    """
    rows = db.execute(
        "SELECT v.* FROM thesis_break_verification v "
        "JOIN thesis_break_event e ON e.event_id=v.event_id "
        "WHERE e.security_id=? AND v.status=? AND v.verified_at<=? "
        "ORDER BY v.verified_at DESC, v.verification_id DESC",
        (security_id, VerificationStatus.VERIFIED.value, as_of),
    ).fetchall()
    for row in rows:
        if row["verification_method"] not in allowed_methods:
            continue
        verified_at = datetime.fromisoformat(row["verified_at"].replace("Z", "+00:00"))
        if datetime.fromisoformat(as_of.replace("Z", "+00:00")) - verified_at > timedelta(
            days=max_age_calendar_days
        ):
            continue
        return dict(row)
    return None

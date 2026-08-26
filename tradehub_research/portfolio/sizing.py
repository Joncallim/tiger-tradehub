"""Deterministic sizing engine, separate from security conviction.

Conviction selects a discrete band; quality/agreement/trajectory supply
discrete multipliers; risk clips reduce the target.  ``target_weight = 0`` /
no action / hold cash are first-class outcomes.  No linear conviction→weight
mapping, no solvers, no adaptive weights.

Arithmetic is integer (ppm / micro-USD / micro-shares).  The raw target is a
single Decimal-free integer product with ONE floor (no cascading truncation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradehub_research.portfolio.policy import PolicySpec
from tradehub_research.portfolio.types import Action, assert_int64

PPM = 1_000_000


@dataclass(frozen=True)
class SizingResult:
    target_weight_ppm: int
    current_weight_ppm: int
    max_quantity_microunits: int
    completion_quantity_microunits: int
    max_notional_microusd: int
    action: Action | None
    reason: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_weight_ppm": self.target_weight_ppm,
            "current_weight_ppm": self.current_weight_ppm,
            "max_quantity_microunits": self.max_quantity_microunits,
            "completion_quantity_microunits": self.completion_quantity_microunits,
            "max_notional_microusd": self.max_notional_microusd,
            "action": self.action.value if self.action else None,
            "reason": self.reason,
            "detail": dict(self.detail),
        }


def _band_value(bands: list[dict[str, int]], metric_ppm: int, field_name: str) -> int:
    """Highest band whose min_ppm <= metric (bands are strictly descending)."""
    for band in bands:
        if metric_ppm >= band["min_ppm"]:
            return band[field_name]
    raise ValueError("no band matched; floor band at min_ppm=0 must exist")


def _floor_to_increment(value: int, increment: int) -> int:
    return (value // increment) * increment


def size_buy(
    policy: PolicySpec,
    *,
    conviction_ppm: int,
    data_quality_ppm: int,
    agreement_ppm: int,
    trajectory: str,
    current_weight_ppm: int,
    nav_microusd: int,
    mark_price_microusd: int,
    quantity_increment_microunits: int,
    clips: dict[str, int],
    available_cash_microusd: int,
    current_quantity_microunits: int,
    min_action_notional_microusd: int,
) -> SizingResult:
    """BUY (ENTER/ADD) sizing: band + multipliers, clipped, floored to increment."""
    sizing = policy.sizing
    base = _band_value(sizing["conviction_bands"], conviction_ppm, "base_target_ppm")
    quality_multiplier = _band_value(sizing["quality_bands"], data_quality_ppm, "multiplier_ppm")
    agreement_multiplier = _band_value(sizing["agreement_bands"], agreement_ppm, "multiplier_ppm")
    trajectory_multiplier = int(sizing["trajectory_multiplier_ppm"][trajectory])
    # Single multiplication, single floor (no cascading truncation bias).
    raw = (base * quality_multiplier * agreement_multiplier * trajectory_multiplier) // (PPM**3)
    raw = _floor_to_increment(raw, int(sizing["weight_increment_ppm"]))

    target = raw
    detail: dict[str, Any] = {
        "base_ppm": base,
        "quality_multiplier_ppm": quality_multiplier,
        "agreement_multiplier_ppm": agreement_multiplier,
        "trajectory_multiplier_ppm": trajectory_multiplier,
        "raw_target_ppm": raw,
    }
    for clip_name, clip_value in sorted(clips.items()):
        if clip_name == "sellable":
            continue
        detail[f"clip_{clip_name}"] = clip_value
        target = min(target, int(clip_value))

    if target <= current_weight_ppm:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_target_not_above_current",
            detail=detail,
        )

    target_weight = _floor_to_increment(target, int(sizing["weight_increment_ppm"]))
    if target_weight <= current_weight_ppm:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_below_increment",
            detail=detail,
        )

    delta_weight_ppm = target_weight - current_weight_ppm
    desired_notional = delta_weight_ppm * nav_microusd // PPM
    notional = min(desired_notional, available_cash_microusd)
    if notional < min_action_notional_microusd:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_below_min_notional",
            detail={
                **detail,
                "desired_notional_microusd": desired_notional,
                "cash_cap_microusd": available_cash_microusd,
            },
        )
    quantity = (notional * PPM) // mark_price_microusd
    quantity = _floor_to_increment(quantity, quantity_increment_microunits)
    if quantity <= 0:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_zero_quantity",
            detail=detail,
        )
    max_notional = (quantity * mark_price_microusd) // PPM
    if max_notional < min_action_notional_microusd:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_max_notional_below_min",
            detail=detail,
        )
    completion = current_quantity_microunits + quantity
    assert_int64(quantity, "max_quantity_microunits")
    assert_int64(max_notional, "max_notional_microusd")
    assert_int64(completion, "completion_quantity_microunits")
    return SizingResult(
        target_weight_ppm=target_weight,
        current_weight_ppm=current_weight_ppm,
        max_quantity_microunits=quantity,
        completion_quantity_microunits=completion,
        max_notional_microusd=max_notional,
        action=Action.BUY,
        reason="buy_eligible",
        detail=detail,
    )


def size_sell(
    policy: PolicySpec,
    *,
    current_weight_ppm: int,
    current_quantity_microunits: int,
    sellable_quantity_microunits: int,
    mark_price_microusd: int,
    nav_microusd: int,
    quantity_increment_microunits: int,
    full_exit: bool,
    min_action_notional_microusd: int,
    adv_microusd: int | None = None,
    max_adv_participation_ppm: int | None = None,
) -> SizingResult:
    """SELL (TRIM/EXIT) sizing: deterministic, long-only, holdings-bounded."""
    sizing = policy.sizing
    detail: dict[str, Any] = {}
    if full_exit:
        target_weight_ppm = 0
        sell_quantity = sellable_quantity_microunits
        detail["mode"] = "full_exit"
    else:
        fraction = int(sizing["trim_remaining_fraction_ppm"])
        target_weight = (current_weight_ppm * fraction) // PPM
        target_weight = _floor_to_increment(target_weight, int(sizing["weight_increment_ppm"]))
        if target_weight >= current_weight_ppm:
            return SizingResult(
                target_weight_ppm=current_weight_ppm,
                current_weight_ppm=current_weight_ppm,
                max_quantity_microunits=0,
                completion_quantity_microunits=current_quantity_microunits,
                max_notional_microusd=0,
                action=None,
                reason="no_action_trim_fraction_not_below_current",
                detail=detail,
            )
        delta_weight_ppm = current_weight_ppm - target_weight
        desired_notional = delta_weight_ppm * nav_microusd // PPM
        sell_quantity = (desired_notional * PPM) // mark_price_microusd
        sell_quantity = _floor_to_increment(sell_quantity, quantity_increment_microunits)
        detail["mode"] = "trim"
        detail["trim_target_ppm"] = target_weight
        target_weight_ppm = target_weight

    sell_quantity = min(sell_quantity, sellable_quantity_microunits)
    if adv_microusd is not None and max_adv_participation_ppm is not None:
        participation_cap = (adv_microusd * max_adv_participation_ppm) // PPM
        sell_notional_cap = participation_cap
        detail["participation_cap_microusd"] = sell_notional_cap
    else:
        sell_notional_cap = None
        detail["participation_cap_microusd"] = None
    max_notional = (sell_quantity * mark_price_microusd) // PPM
    if sell_notional_cap is not None:
        max_notional = min(max_notional, sell_notional_cap)
        capped_quantity = (max_notional * PPM) // mark_price_microusd
        capped_quantity = _floor_to_increment(capped_quantity, quantity_increment_microunits)
        if capped_quantity < sell_quantity:
            sell_quantity = capped_quantity
            max_notional = (sell_quantity * mark_price_microusd) // PPM
    if sell_quantity <= 0 or max_notional <= 0:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_zero_sell",
            detail=detail,
        )
    if max_notional < min_action_notional_microusd:
        return SizingResult(
            target_weight_ppm=current_weight_ppm,
            current_weight_ppm=current_weight_ppm,
            max_quantity_microunits=0,
            completion_quantity_microunits=current_quantity_microunits,
            max_notional_microusd=0,
            action=None,
            reason="no_action_sell_below_min_notional",
            detail=detail,
        )
    if full_exit and sell_quantity < current_quantity_microunits:
        # Infeasible full EXIT (not enough trusted sellable capacity): degrade
        # deterministically to a TRIM proposal.
        return size_sell(
            policy,
            current_weight_ppm=current_weight_ppm,
            current_quantity_microunits=current_quantity_microunits,
            sellable_quantity_microunits=sell_quantity,
            mark_price_microusd=mark_price_microusd,
            nav_microusd=nav_microusd,
            quantity_increment_microunits=quantity_increment_microunits,
            full_exit=False,
            min_action_notional_microusd=min_action_notional_microusd,
            adv_microusd=adv_microusd,
            max_adv_participation_ppm=max_adv_participation_ppm,
        )
    completion = current_quantity_microunits - sell_quantity
    assert_int64(sell_quantity, "max_quantity_microunits")
    assert_int64(max_notional, "max_notional_microusd")
    assert_int64(completion, "completion_quantity_microunits")
    return SizingResult(
        target_weight_ppm=target_weight_ppm,
        current_weight_ppm=current_weight_ppm,
        max_quantity_microunits=sell_quantity,
        completion_quantity_microunits=completion,
        max_notional_microusd=max_notional,
        action=Action.SELL,
        reason="sell_eligible",
        detail=detail,
    )

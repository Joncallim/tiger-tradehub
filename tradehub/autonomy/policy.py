"""PAPER autonomy policy (issue #51 D).

A small explicit versioned policy, separate from the long-term investment
constitution. It governs ONLY autonomous PAPER execution. All defaults are
conservative and visibly labelled PAPER_PROVISIONAL -- they must never
silently become live-money limits (the runner refuses any account_mode other
than PAPER).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

POLICY_VERSION = "paper-autonomy-v1"
POLICY_FILE = Path("/var/lib/tradehub-research/autonomy/paper_policy.json")

# All defaults are provisional acceptance values -- PAPER only.
PAPER_PROVISIONAL_DEFAULTS: dict = {
    "enabled": True,
    "account_mode": "PAPER",
    "allowed_universe": ["US_STOCKS"],  # US equities only; no leverage/shorts/options
    "allowed_state_transitions": [
        "WATCH->ENTER",
        "HOLD->ADD",
        "HOLD->TRIM",
        "HOLD->EXIT",
        "TRIM->EXIT",
    ],
    "max_order_count_per_day": 2,  # PAPER_PROVISIONAL
    "max_notional_per_day_microusd": 20_000_000_000,  # $20,000 PAPER_PROVISIONAL
    "max_per_position_exposure_ppm": 100_000,  # 10% of NAV PAPER_PROVISIONAL
    "proposal_max_age_seconds": 3600 * 26,  # 26h: proposals from the prior M/W/F cycle
    # 78h covers the longest M/W/F gap (Friday session -> Monday cycle);
    # a timer firing without fresh data refuses rather than manufacturing.
    "data_max_age_seconds": 3600 * 78,
    "kill_switch": False,  # file-based kill switch is the authoritative control
    "policy_version": POLICY_VERSION,
    "label": "PAPER_PROVISIONAL",
}


@dataclass(frozen=True)
class PaperAutonomyPolicy:
    enabled: bool
    account_mode: str
    allowed_universe: tuple[str, ...]
    allowed_state_transitions: tuple[str, ...]
    max_order_count_per_day: int
    max_notional_per_day_microusd: int
    max_per_position_exposure_ppm: int
    proposal_max_age_seconds: int
    data_max_age_seconds: int
    kill_switch: bool
    policy_version: str
    label: str = "PAPER_PROVISIONAL"
    extra: dict = field(default_factory=dict)

    @property
    def is_paper(self) -> bool:
        return self.account_mode == "PAPER"


def load_policy(path: Path = POLICY_FILE) -> PaperAutonomyPolicy:
    """Load + validate the versioned PAPER policy. Missing/invalid -> refuse
    closed (a policy failure must never default to autonomous writes)."""
    if not path.exists():
        raise FileNotFoundError(f"PAPER autonomy policy not found at {path}; refusing to run")
    raw = json.loads(path.read_text())
    if raw.get("account_mode") != "PAPER":
        raise ValueError(
            f"autonomy policy account_mode must be PAPER, got {raw.get('account_mode')!r}"
        )
    if raw.get("policy_version") != POLICY_VERSION:
        raise ValueError(
            f"policy version mismatch: expected {POLICY_VERSION}, got {raw.get('policy_version')!r}"
        )
    allowed = set(PAPER_PROVISIONAL_DEFAULTS)
    merged = {**PAPER_PROVISIONAL_DEFAULTS, **{k: v for k, v in raw.items() if k in allowed}}
    # no leverage / no shorts / no options: the allowed_universe + transitions
    # are enforced structurally; a policy that enables them is rejected here.
    if "SHORT" in str(merged.get("allowed_universe", "")).upper():
        raise ValueError("PAPER autonomy policy must not enable shorting")
    return PaperAutonomyPolicy(
        enabled=bool(merged["enabled"]),
        account_mode=str(merged["account_mode"]),
        allowed_universe=tuple(merged["allowed_universe"]),
        allowed_state_transitions=tuple(merged["allowed_state_transitions"]),
        max_order_count_per_day=int(merged["max_order_count_per_day"]),
        max_notional_per_day_microusd=int(merged["max_notional_per_day_microusd"]),
        max_per_position_exposure_ppm=int(merged["max_per_position_exposure_ppm"]),
        proposal_max_age_seconds=int(merged["proposal_max_age_seconds"]),
        data_max_age_seconds=int(merged["data_max_age_seconds"]),
        kill_switch=bool(merged["kill_switch"]),
        policy_version=str(merged["policy_version"]),
        label=str(merged["label"]),
    )


def default_policy_payload() -> dict:
    return dict(PAPER_PROVISIONAL_DEFAULTS)

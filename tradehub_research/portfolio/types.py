"""Shared portfolio-plane types, enums, and deterministic primitives.

Everything in this module is immutable and deterministic.  Integer micro-units
(micro-USD, ppm, micro-shares) are the only numeric representation crossing the
portfolio-plane boundary; binary float is never used for money/weight math.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum

from tradehub_research.screens import canonical_json

# ---------------------------------------------------------------------------
# Canonical state machine
# ---------------------------------------------------------------------------


class State(str, Enum):
    DISCOVER = "DISCOVER"
    WATCH = "WATCH"
    ENTER = "ENTER"
    HOLD = "HOLD"
    ADD = "ADD"
    TRIM = "TRIM"
    EXIT = "EXIT"


STATES: tuple[State, ...] = tuple(State)

# The 11 canonical edges (architecture doc §13).
CANONICAL_EDGES: frozenset[tuple[State, State]] = frozenset(
    {
        (State.DISCOVER, State.WATCH),
        (State.WATCH, State.DISCOVER),
        (State.WATCH, State.ENTER),
        (State.ENTER, State.HOLD),
        (State.ADD, State.HOLD),
        (State.HOLD, State.ADD),
        (State.HOLD, State.TRIM),
        (State.TRIM, State.HOLD),
        (State.TRIM, State.EXIT),
        (State.HOLD, State.EXIT),
        (State.EXIT, State.WATCH),
    }
)

# Edges that carry an actionable BUY/SELL paper proposal.
ACTIONABLE_EDGES: frozenset[tuple[State, State]] = frozenset(
    {
        (State.WATCH, State.ENTER),  # BUY
        (State.HOLD, State.ADD),  # BUY
        (State.HOLD, State.TRIM),  # SELL
        (State.HOLD, State.EXIT),  # SELL
        (State.TRIM, State.EXIT),  # SELL
    }
)

# Pending recommendation states that settle against a trusted snapshot
# quantity reaching the originating proposal's completion quantity.
PENDING_STATES: frozenset[State] = frozenset({State.ENTER, State.ADD, State.TRIM, State.EXIT})


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


SELL_REASONS: frozenset[str] = frozenset(
    {
        "thesis_broken",
        "thesis_realised",
        "opportunity_cost",
        "risk_reduction",
        "data_integrity",
        "policy_ineligible",
    }
)

# Asymmetric sell-reason provenance is code-fixed (never LLM-supplied).
TRIGGER_TO_REASON: dict[str, str] = {
    "VERIFIED_THESIS_BREAK": "thesis_broken",
    "THESIS_REALISED": "thesis_realised",
    "RISK_REDUCTION": "risk_reduction",
    "DATA_INTEGRITY": "data_integrity",
    "POLICY_INELIGIBLE": "policy_ineligible",
    "SCORE_BAND": "score_band",
}

# change_cause values that count as evidence-driven observations for
# persistence/hysteresis.  Rebase/rerun/correction causes never count.
EVIDENCE_DRIVEN_CAUSES: frozenset[str] = frozenset({"EVIDENCE_DRIVEN"})

CHANGE_CAUSES: frozenset[str] = frozenset(
    {
        "INITIAL",
        "EVIDENCE_DRIVEN",
        "CORRECTION_RESTATEMENT",
        "SCORING_VERSION_CHANGE",
        "SCREEN_METHODOLOGY_CHANGE",
        "MODEL_REASSESSMENT",
    }
)

TRAJECTORY_LABELS: frozenset[str] = frozenset({"INITIAL", "REBASED", "RISING", "FALLING", "STABLE"})

SECTOR_COVERAGE_STATUSES: frozenset[str] = frozenset({"SUPPORTED", "LIMITED", "RESEARCH_ONLY"})


class TriggerKind(str, Enum):
    SCORE_BAND = "SCORE_BAND"
    THESIS_REALISED = "THESIS_REALISED"
    OPPORTUNITY_COST = "OPPORTUNITY_COST"
    POLICY_INELIGIBLE = "POLICY_INELIGIBLE"
    DATA_INTEGRITY = "DATA_INTEGRITY"
    RISK_REDUCTION = "RISK_REDUCTION"
    VERIFIED_THESIS_BREAK = "VERIFIED_THESIS_BREAK"


class PositionRequirement(str, Enum):
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    ANY = "ANY"


class PolicyStatus(str, Enum):
    FIXTURE = "FIXTURE"
    PROVISIONAL = "PROVISIONAL"
    PAPER = "PAPER"


class SignalStatus(str, Enum):
    PASS = "PASS"
    INELIGIBLE = "INELIGIBLE"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"


class RiskStatus(str, Enum):
    PASS = "PASS"
    LIMITED = "LIMITED"
    UNKNOWN = "UNKNOWN"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class FinalStatus(str, Enum):
    TRANSITIONED = "TRANSITIONED"
    PROPOSED = "PROPOSED"
    NO_ACTION = "NO_ACTION"
    BLOCKED = "BLOCKED"


class ValueStatus(str, Enum):
    KNOWN = "KNOWN"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class VerificationMethod(str, Enum):
    OWNER_ATTESTED = "OWNER_ATTESTED"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    FIXTURE = "FIXTURE"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class SignalSourceKind(str, Enum):
    FIXTURE = "FIXTURE"
    COMMITTEE_STRUCTURED = "COMMITTEE_STRUCTURED"
    DETERMINISTIC_METRIC = "DETERMINISTIC_METRIC"
    OWNER_ATTESTED = "OWNER_ATTESTED"


# ---------------------------------------------------------------------------
# Deterministic hashing (mirrors the committee-plane convention)
# ---------------------------------------------------------------------------


def C(value: object) -> str:
    """Canonical content hash: sha256 of RFC 8785-style JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def D(tag: str, material: str) -> str:
    """Deterministic identity: sha256 of (tag + NUL + material)."""
    return hashlib.sha256((f"{tag}\0{material}").encode()).hexdigest()


def C_json_text(value: str) -> str:
    """Content hash over an already-canonical JSON text (for stored spec rows)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_roundtrip(value: object) -> str:
    """Deterministic JSON text for storage; rejects NaN/Infinity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


# ---------------------------------------------------------------------------
# Integer micro-unit arithmetic guards
# ---------------------------------------------------------------------------

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def assert_int64(value: int, what: str) -> None:
    """SQLite INTEGER is signed 64-bit; refuse to store anything outside it."""
    if not INT64_MIN <= value <= INT64_MAX:
        raise OverflowError(f"{what} ({value}) exceeds signed 64-bit range")


def ppm(ratio: float) -> int:
    """Convert a 0..1 float ratio to integer parts-per-million (validated)."""
    if not 0.0 <= ratio <= 1.0:
        raise ValueError(f"ratio out of range: {ratio}")
    return int(round(ratio * 1_000_000))


def microusd(dollars: float) -> int:
    """Convert a dollar float to integer micro-USD (validation boundary only)."""
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(str(dollars)) * Decimal(1_000_000)).quantize(Decimal(1), ROUND_HALF_UP))


def micromult(value: float) -> int:
    """Convert a float multiplier (e.g. split factor) to integer 1e12 units."""
    from decimal import ROUND_HALF_UP, Decimal

    return int(
        (Decimal(str(value)) * Decimal(1_000_000_000_000)).quantize(Decimal(1), ROUND_HALF_UP)
    )

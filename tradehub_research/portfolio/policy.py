"""Versioned portfolio policy contract and registry.

Investment doctrine lives ONLY in a registered, versioned policy row
(``portfolio_policy``).  No numeric investment limit is hardcoded in Python.
Unknown or malformed policy versions FAIL CLOSED before any run row is written.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.portfolio.types import (
    CANONICAL_EDGES,
    C_json_text,
    PolicyStatus,
    State,
    json_roundtrip,
)

POLICY_SCHEMA_VERSION = 1

# Exact top-level keys; extras are rejected.
REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "eligibility_rules",
    "transition_controls",
    "material_change",
    "thesis_break",
    "settlement",
    "sizing",
    "risk",
    "budget",
    "order_constraints",
)

TRIGGER_KINDS = (
    "SCORE_BAND",
    "THESIS_REALISED",
    "POLICY_INELIGIBLE",
    "DATA_INTEGRITY",
    "RISK_REDUCTION",
    "VERIFIED_THESIS_BREAK",
)

POSITION_REQUIREMENTS = ("ABSENT", "PRESENT", "ANY")

EDGE_KEYS = frozenset(f"{edge[0].value}_{edge[1].value}" for edge in CANONICAL_EDGES)

# Edges that must require persistence evidence (hysteresis is load-bearing).
PERSISTENCE_REQUIRED_EDGES = frozenset(
    {
        "WATCH_ENTER",
        "HOLD_ADD",
        "HOLD_TRIM",
        "HOLD_EXIT",
        "TRIM_EXIT",
    }
)

MAX_PPM = 1_000_000


def _no_floats(value: Any, path: str) -> None:
    """Reject binary float anywhere in a policy spec (money/weight must be int ppm)."""
    if isinstance(value, float):
        raise ValueError(f"policy field {path} must be an integer, not a float")
    if isinstance(value, dict):
        for key, item in value.items():
            _no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_floats(item, f"{path}[{index}]")


def _ppm_field(value: Any, path: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"policy field {path} must be an integer ppm, got {value!r}")
    if not 0 <= value <= MAX_PPM:
        raise ValueError(f"policy field {path} out of ppm range: {value}")
    return value


def _bands(value: Any, path: str, field_name: str) -> list[dict[str, int]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"policy field {path} must be a non-empty list")
    bands: list[dict[str, int]] = []
    seen_min: set[int] = set()
    previous_min = MAX_PPM + 1
    for index, band in enumerate(value):
        if not isinstance(band, dict):
            raise ValueError(f"policy field {path}[{index}] must be an object")
        minimum = _ppm_field(band.get("min_ppm"), f"{path}[{index}].min_ppm")
        if minimum is None:
            raise ValueError(f"policy field {path}[{index}].min_ppm required")
        if minimum in seen_min:
            raise ValueError(f"duplicate min_ppm in {path}")
        if minimum >= previous_min:
            raise ValueError(f"{path} bands must be strictly descending by min_ppm")
        seen_min.add(minimum)
        previous_min = minimum
        amount = _ppm_field(band.get(field_name), f"{path}[{index}].{field_name}")
        bands.append({"min_ppm": minimum, field_name: amount})
    if bands[-1]["min_ppm"] != 0:
        raise ValueError(f"{path} must include a floor band at min_ppm=0")
    return bands


@dataclass(frozen=True)
class PolicySpec:
    """Validated, canonical portfolio policy.

    Attributes mirror the JSON contract exactly; ``as_dict`` returns the
    canonical stored form (``spec_json`` text is derived from it).
    """

    policy_version: str
    policy_status: PolicyStatus
    sizing_policy_version: str
    eligibility_rules: tuple[dict[str, Any], ...]
    transition_controls: dict[str, dict[str, int | bool]]
    material_change: dict[str, Any]
    thesis_break: dict[str, Any]
    settlement: dict[str, int]
    sizing: dict[str, Any]
    risk: dict[str, Any]
    budget: dict[str, Any]
    order_constraints: dict[str, Any]
    approved_by: str | None = None
    approved_at: str | None = None

    def __post_init__(self) -> None:
        _no_floats(self.as_dict(), "spec")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "eligibility_rules": list(self.eligibility_rules),
            "transition_controls": dict(self.transition_controls),
            "material_change": dict(self.material_change),
            "thesis_break": dict(self.thesis_break),
            "settlement": dict(self.settlement),
            "sizing": dict(self.sizing),
            "risk": dict(self.risk),
            "budget": dict(self.budget),
            "order_constraints": dict(self.order_constraints),
        }

    @property
    def spec_json(self) -> str:
        return json_roundtrip(self.as_dict())

    @property
    def spec_hash(self) -> str:
        return C_json_text(self.spec_json)

    def transition_control(self, from_state: State, to_state: State) -> dict[str, int | bool]:
        key = f"{from_state.value}_{to_state.value}"
        try:
            return self.transition_controls[key]
        except KeyError as exc:
            raise KeyError(f"no transition control for {key}") from exc

    def persistence_required(self, from_state: State, to_state: State) -> int:
        return int(self.transition_control(from_state, to_state)["required_evidence_observations"])

    def cooldown_days(self, from_state: State, to_state: State) -> int:
        return int(self.transition_control(from_state, to_state)["cooldown_calendar_days"])

    def allows_material_bypass(self, from_state: State, to_state: State) -> bool:
        return bool(self.transition_control(from_state, to_state)["allow_material_change_bypass"])

    def allows_verified_break_bypass(self, from_state: State, to_state: State) -> bool:
        return bool(self.transition_control(from_state, to_state)["allow_verified_break_bypass"])


def validate_spec(spec: dict[str, Any]) -> None:
    """Validate a raw policy dict; raises ValueError on any violation."""
    if not isinstance(spec, dict):
        raise ValueError("policy spec must be a JSON object")
    if set(spec.keys()) != set(REQUIRED_TOP_LEVEL_KEYS):
        raise ValueError(
            f"policy spec keys must be exactly {sorted(REQUIRED_TOP_LEVEL_KEYS)}; "
            f"got {sorted(spec.keys())}"
        )
    if spec["schema_version"] != POLICY_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported policy schema_version {spec['schema_version']!r}; "
            f"expected {POLICY_SCHEMA_VERSION}"
        )
    _no_floats(spec, "spec")

    # --- eligibility rules -------------------------------------------------
    rules = spec["eligibility_rules"]
    if not isinstance(rules, list) or not rules:
        raise ValueError("eligibility_rules must be a non-empty list")
    seen_ids: set[str] = set()
    seen_priorities: set[int] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"eligibility_rules[{index}] must be an object")
        path = f"eligibility_rules[{index}]"
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError(f"{path}.rule_id must be a non-empty string")
        if rule_id in seen_ids:
            raise ValueError(f"duplicate rule_id {rule_id!r}")
        seen_ids.add(rule_id)
        priority = rule.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ValueError(f"{path}.priority must be an integer")
        if priority in seen_priorities:
            raise ValueError(f"duplicate priority {priority} in eligibility_rules")
        seen_priorities.add(priority)
        from_state = rule.get("from_state")
        to_state = rule.get("to_state")
        try:
            edge = (State(from_state), State(to_state))
        except ValueError as exc:
            raise ValueError(f"{path} has invalid states {from_state!r}->{to_state!r}") from exc
        if edge not in CANONICAL_EDGES:
            raise ValueError(f"{path} edge {from_state!r}->{to_state!r} is not canonical")
        if rule.get("trigger_kind") not in TRIGGER_KINDS:
            raise ValueError(f"{path}.trigger_kind invalid: {rule.get('trigger_kind')!r}")
        _ppm_field(rule.get("conviction_min_ppm"), f"{path}.conviction_min_ppm")
        conviction_max = _ppm_field(rule.get("conviction_max_ppm"), f"{path}.conviction_max_ppm")
        conviction_min = _ppm_field(rule.get("conviction_min_ppm"), f"{path}.conviction_min_ppm")
        if (
            conviction_min is not None
            and conviction_max is not None
            and conviction_min > conviction_max
        ):
            raise ValueError(f"{path} conviction_min_ppm exceeds conviction_max_ppm")
        _ppm_field(rule.get("data_quality_min_ppm"), f"{path}.data_quality_min_ppm")
        _ppm_field(rule.get("agreement_min_ppm"), f"{path}.agreement_min_ppm")
        trajectories = rule.get("trajectories", [])
        if not isinstance(trajectories, list) or any(
            label not in ("INITIAL", "REBASED", "RISING", "FALLING", "STABLE")
            for label in trajectories
        ):
            raise ValueError(f"{path}.trajectories invalid: {trajectories!r}")
        _ppm_field(rule.get("opportunity_min_ppm"), f"{path}.opportunity_min_ppm", allow_none=True)
        _ppm_field(rule.get("opportunity_max_ppm"), f"{path}.opportunity_max_ppm", allow_none=True)
        opportunity_min = rule.get("opportunity_min_ppm")
        opportunity_max = rule.get("opportunity_max_ppm")
        if (
            opportunity_min is not None
            and opportunity_max is not None
            and opportunity_min > opportunity_max
        ):
            raise ValueError(f"{path} opportunity_min_ppm exceeds opportunity_max_ppm")
        if rule.get("position") not in POSITION_REQUIREMENTS:
            raise ValueError(f"{path}.position invalid: {rule.get('position')!r}")
        coverage = rule.get("allowed_sector_coverage_statuses", [])
        if (
            not isinstance(coverage, list)
            or not coverage
            or any(item not in ("SUPPORTED", "LIMITED", "RESEARCH_ONLY") for item in coverage)
        ):
            raise ValueError(f"{path}.allowed_sector_coverage_statuses invalid: {coverage!r}")

    # --- transition controls ----------------------------------------------
    controls = spec["transition_controls"]
    if not isinstance(controls, dict) or set(controls.keys()) != EDGE_KEYS:
        raise ValueError(
            f"transition_controls must cover exactly the {len(EDGE_KEYS)} canonical edges"
        )
    for edge_key, control in controls.items():
        if not isinstance(control, dict):
            raise ValueError(f"transition_controls.{edge_key} must be an object")
        required_observations = control.get("required_evidence_observations")
        if (
            not isinstance(required_observations, int)
            or isinstance(required_observations, bool)
            or required_observations < 0
        ):
            raise ValueError(
                f"transition_controls.{edge_key}.required_evidence_observations invalid"
            )
        if edge_key in PERSISTENCE_REQUIRED_EDGES and required_observations < 1:
            raise ValueError(
                f"transition_controls.{edge_key} must require >=1 evidence observation"
            )
        cooldown = control.get("cooldown_calendar_days")
        if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
            raise ValueError(f"transition_controls.{edge_key}.cooldown_calendar_days invalid")
        for flag in ("allow_material_change_bypass", "allow_verified_break_bypass"):
            if not isinstance(control.get(flag), bool):
                raise ValueError(f"transition_controls.{edge_key}.{flag} must be boolean")

    # --- material change ---------------------------------------------------
    material = spec["material_change"]
    if not isinstance(material, dict):
        raise ValueError("material_change must be an object")
    delta = _ppm_field(material.get("conviction_delta_ppm"), "material_change.conviction_delta_ppm")
    if delta is None or delta == 0:
        raise ValueError("material_change.conviction_delta_ppm must be > 0")
    directions = material.get("direction_by_edge")
    if not isinstance(directions, dict):
        raise ValueError("material_change.direction_by_edge must be an object")
    expected_direction_edges = frozenset(
        {
            key
            for key in EDGE_KEYS
            if key in ("WATCH_ENTER", "HOLD_ADD", "HOLD_TRIM", "HOLD_EXIT", "TRIM_EXIT")
        }
    )
    if set(directions.keys()) != expected_direction_edges:
        raise ValueError(
            f"material_change.direction_by_edge must cover {sorted(expected_direction_edges)}"
        )
    for edge_key, direction in directions.items():
        if direction not in ("UP", "DOWN", "NONE"):
            raise ValueError(f"material_change.direction_by_edge.{edge_key} invalid: {direction!r}")

    # --- thesis break ------------------------------------------------------
    thesis_break = spec["thesis_break"]
    if not isinstance(thesis_break, dict):
        raise ValueError("thesis_break must be an object")
    methods = thesis_break.get("allowed_verification_methods")
    if (
        not isinstance(methods, list)
        or not methods
        or any(
            method not in ("OWNER_ATTESTED", "DETERMINISTIC_RULE", "FIXTURE") for method in methods
        )
    ):
        raise ValueError("thesis_break.allowed_verification_methods invalid")
    max_age = thesis_break.get("max_age_calendar_days")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
        raise ValueError("thesis_break.max_age_calendar_days must be > 0")
    realised_max = thesis_break.get("realised_opportunity_max_ppm", 0)
    _ppm_field(realised_max, "thesis_break.realised_opportunity_max_ppm")
    opportunity_cost_max = thesis_break.get("opportunity_cost_max_ppm", 0)
    _ppm_field(opportunity_cost_max, "thesis_break.opportunity_cost_max_ppm")

    # --- settlement ---------------------------------------------------------
    settlement = spec["settlement"]
    if not isinstance(settlement, dict):
        raise ValueError("settlement must be an object")
    tolerance = settlement.get("quantity_tolerance_microunits")
    if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
        raise ValueError("settlement.quantity_tolerance_microunits must be >= 0")
    pending_max = settlement.get("pending_max_calendar_days")
    if not isinstance(pending_max, int) or isinstance(pending_max, bool) or pending_max < 0:
        raise ValueError("settlement.pending_max_calendar_days must be >= 0")

    # --- sizing -------------------------------------------------------------
    sizing = spec["sizing"]
    if not isinstance(sizing, dict):
        raise ValueError("sizing must be an object")
    if (
        not isinstance(sizing.get("sizing_policy_version"), str)
        or not sizing["sizing_policy_version"].strip()
    ):
        raise ValueError("sizing.sizing_policy_version must be a non-empty string")
    _bands(sizing.get("conviction_bands"), "sizing.conviction_bands", "base_target_ppm")
    _bands(sizing.get("quality_bands"), "sizing.quality_bands", "multiplier_ppm")
    _bands(sizing.get("agreement_bands"), "sizing.agreement_bands", "multiplier_ppm")
    trajectory_multiplier = sizing.get("trajectory_multiplier_ppm")
    if not isinstance(trajectory_multiplier, dict) or set(trajectory_multiplier.keys()) != {
        "INITIAL",
        "REBASED",
        "RISING",
        "FALLING",
        "STABLE",
    }:
        raise ValueError("sizing.trajectory_multiplier_ppm must cover all five labels")
    for label, multiplier in trajectory_multiplier.items():
        _ppm_field(multiplier, f"sizing.trajectory_multiplier_ppm.{label}")
    trim_fraction = _ppm_field(
        sizing.get("trim_remaining_fraction_ppm"), "sizing.trim_remaining_fraction_ppm"
    )
    increment = sizing.get("weight_increment_ppm")
    if not isinstance(increment, int) or isinstance(increment, bool) or increment <= 0:
        raise ValueError("sizing.weight_increment_ppm must be > 0")
    min_action = sizing.get("min_action_notional_microusd")
    if not isinstance(min_action, int) or isinstance(min_action, bool) or min_action < 0:
        raise ValueError("sizing.min_action_notional_microusd must be >= 0")
    if trim_fraction is None:
        raise ValueError("sizing.trim_remaining_fraction_ppm required")

    # --- risk ---------------------------------------------------------------
    risk = spec["risk"]
    if not isinstance(risk, dict):
        raise ValueError("risk must be an object")
    _ppm_field(risk.get("max_position_ppm"), "risk.max_position_ppm")
    _ppm_field(risk.get("max_sector_ppm"), "risk.max_sector_ppm")
    _ppm_field(risk.get("max_active_signal_book_ppm"), "risk.max_active_signal_book_ppm")
    for key in (
        "price_stale_calendar_days",
        "return_window_sessions",
        "min_return_observations",
        "volatility_window_sessions",
        "min_vol_observations",
        "annualization_sessions",
        "correlation_window_sessions",
        "min_overlap_observations",
        "adv_window_sessions",
        "min_adv_observations",
    ):
        value = risk.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"risk.{key} must be a positive integer")
    _ppm_field(risk.get("max_annualized_vol_ppm"), "risk.max_annualized_vol_ppm")
    _ppm_field(risk.get("correlation_threshold_ppm"), "risk.correlation_threshold_ppm")
    _ppm_field(risk.get("max_correlated_book_ppm"), "risk.max_correlated_book_ppm")
    _ppm_field(risk.get("min_correlated_holding_ppm"), "risk.min_correlated_holding_ppm")
    _ppm_field(risk.get("max_adv_participation_ppm"), "risk.max_adv_participation_ppm")
    _ppm_field(risk.get("max_position_adv_days_ppm"), "risk.max_position_adv_days_ppm")
    _ppm_field(risk.get("snapshot_tolerance_ppm"), "risk.snapshot_tolerance_ppm")
    for key in ("factor_required", "drawdown_required"):
        if not isinstance(risk.get(key), bool):
            raise ValueError(f"risk.{key} must be boolean")
    if risk.get("unknown_increase") != "BLOCK":
        raise ValueError("risk.unknown_increase must be 'BLOCK' (fail closed on increases)")
    if risk.get("unknown_decrease") not in ("LIMITED", "BLOCK"):
        raise ValueError("risk.unknown_decrease must be 'LIMITED' or 'BLOCK'")
    if risk.get("min_overlap_observations", 0) > risk.get("correlation_window_sessions", 0):
        raise ValueError("risk.min_overlap_observations cannot exceed correlation_window_sessions")
    if risk.get("min_vol_observations", 0) < 2:
        raise ValueError(
            "risk.min_vol_observations must be >= 2 (1-observation variance is undefined)"
        )
    if risk.get("min_vol_observations", 0) > risk.get("volatility_window_sessions", 0):
        raise ValueError("risk.min_vol_observations cannot exceed volatility_window_sessions")
    if risk.get("min_return_observations", 0) < 1:
        raise ValueError("risk.min_return_observations must be >= 1")
    if risk.get("min_return_observations", 0) > risk.get("return_window_sessions", 0):
        raise ValueError("risk.min_return_observations cannot exceed return_window_sessions")
    if risk.get("min_adv_observations", 0) > risk.get("adv_window_sessions", 0):
        raise ValueError("risk.min_adv_observations cannot exceed adv_window_sessions")

    # --- budget -------------------------------------------------------------
    budget = spec["budget"]
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    if budget.get("timezone") != "UTC":
        raise ValueError("budget.timezone must be 'UTC'")
    max_count = budget.get("max_actionable_count")
    if not isinstance(max_count, int) or isinstance(max_count, bool) or max_count < 0:
        raise ValueError("budget.max_actionable_count must be >= 0")
    max_notional = budget.get("max_notional_microusd")
    if not isinstance(max_notional, int) or isinstance(max_notional, bool) or max_notional < 0:
        raise ValueError("budget.max_notional_microusd must be >= 0")
    for key in ("category_priority", "reason_priority"):
        priority = budget.get(key)
        if (
            not isinstance(priority, list)
            or not priority
            or any(not isinstance(item, str) for item in priority)
        ):
            raise ValueError(f"budget.{key} must be a non-empty list of strings")

    # --- order constraints ---------------------------------------------------
    constraints = spec["order_constraints"]
    if not isinstance(constraints, dict):
        raise ValueError("order_constraints must be an object")
    increment = constraints.get("quantity_increment_microunits")
    if not isinstance(increment, int) or isinstance(increment, bool) or increment <= 0:
        raise ValueError("order_constraints.quantity_increment_microunits must be > 0")
    if not isinstance(constraints.get("limit_only"), bool):
        raise ValueError("order_constraints.limit_only must be boolean")


def build_policy(
    policy_version: str,
    policy_status: PolicyStatus,
    spec: dict[str, Any],
    *,
    approved_by: str | None = None,
    approved_at: str | None = None,
) -> PolicySpec:
    """Validate and construct a canonical PolicySpec (no DB writes)."""
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("policy_version must be a non-empty string")
    validate_spec(spec)
    if policy_status == PolicyStatus.PAPER:
        if not approved_by or not approved_at:
            raise ValueError("PAPER policy requires approved_by and approved_at")
    elif approved_by or approved_at:
        raise ValueError("FIXTURE/PROVISIONAL policies must not carry approval fields")
    # FIXTURE verification methods are only valid inside FIXTURE policies
    if policy_status != PolicyStatus.FIXTURE and "FIXTURE" in spec["thesis_break"].get(
        "allowed_verification_methods", []
    ):
        raise ValueError(
            "FIXTURE thesis-break verification method is not allowed in "
            f"{policy_status.value} policies"
        )
    return PolicySpec(
        policy_version=policy_version,
        policy_status=policy_status,
        sizing_policy_version=str(spec["sizing"]["sizing_policy_version"]),
        eligibility_rules=tuple(copy.deepcopy(rule) for rule in spec["eligibility_rules"]),
        transition_controls=copy.deepcopy(spec["transition_controls"]),
        material_change=copy.deepcopy(spec["material_change"]),
        thesis_break=copy.deepcopy(spec["thesis_break"]),
        settlement=copy.deepcopy(spec["settlement"]),
        sizing=copy.deepcopy(spec["sizing"]),
        risk=copy.deepcopy(spec["risk"]),
        budget=copy.deepcopy(spec["budget"]),
        order_constraints=copy.deepcopy(spec["order_constraints"]),
        approved_by=approved_by,
        approved_at=approved_at,
    )


def load_policy_from_json(policy_version: str, policy_status: PolicyStatus, raw: str) -> PolicySpec:
    """Parse a JSON text policy and build a validated PolicySpec."""
    import json

    parsed = json.loads(raw)
    return build_policy(policy_version, policy_status, parsed)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PolicyRegistry:
    """Append-only, equality-checked registry of versioned policies."""

    def __init__(self, database: ResearchDB):
        self.database = database

    def register(self, policy: PolicySpec, *, created_at: str | None = None) -> str:
        """Insert a policy row idempotently; reject byte-mismatched duplicates."""
        created_at = created_at or utc_now()
        with self.database.connect() as db:
            existing = db.execute(
                "SELECT spec_json,policy_status,sizing_policy_version FROM portfolio_policy "
                "WHERE policy_version=?",
                (policy.policy_version,),
            ).fetchone()
            if existing is not None:
                if existing["spec_json"] != policy.spec_json:
                    raise ValueError(
                        f"policy_version {policy.policy_version!r} already registered with "
                        "different spec content"
                    )
                if existing["policy_status"] != policy.policy_status.value:
                    raise ValueError(
                        f"policy_version {policy.policy_version!r} already registered with "
                        f"status {existing['policy_status']}"
                    )
                return policy.policy_version
            duplicate_hash = db.execute(
                "SELECT policy_version FROM portfolio_policy WHERE spec_hash=?",
                (policy.spec_hash,),
            ).fetchone()
            if duplicate_hash is not None:
                raise ValueError(
                    f"spec hash collision: {policy.policy_version!r} duplicates "
                    f"{duplicate_hash['policy_version']!r}"
                )
            db.execute(
                "INSERT INTO portfolio_policy("
                "policy_version,policy_status,sizing_policy_version,spec_json,spec_hash,"
                "approved_by,approved_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    policy.policy_version,
                    policy.policy_status.value,
                    policy.sizing_policy_version,
                    policy.spec_json,
                    policy.spec_hash,
                    policy.approved_by,
                    policy.approved_at,
                    created_at,
                ),
            )
        return policy.policy_version

    def get(self, policy_version: str) -> PolicySpec:
        """Load a registered policy; unknown version raises KeyError (FAIL CLOSED)."""
        with self.database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT * FROM portfolio_policy WHERE policy_version=?", (policy_version,)
            ).fetchone()
        if row is None:
            raise KeyError(f"no registered portfolio policy version {policy_version!r}")
        return build_policy(
            row["policy_version"],
            PolicyStatus(row["policy_status"]),
            json_roundtrip_load(row["spec_json"]),
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
        )

    def status_gate(self, policy_version: str, *, allow_provisional: bool) -> PolicySpec:
        """Load a policy and enforce its status for a real (non-test) run.

        FIXTURE policies are never acceptable for a real run; PROVISIONAL
        requires the explicit CLI opt-in; PAPER is owner-approved.
        """
        policy = self.get(policy_version)
        if policy.policy_status == PolicyStatus.FIXTURE:
            raise ValueError(f"policy {policy_version!r} is FIXTURE and cannot drive a real run")
        if policy.policy_status == PolicyStatus.PROVISIONAL and not allow_provisional:
            raise ValueError(
                f"policy {policy_version!r} is PROVISIONAL; pass --allow-provisional to accept "
                "the labelled PAPER/PROVISIONAL risk posture"
            )
        return policy


def json_roundtrip_load(text: str) -> dict[str, Any]:
    import json

    return json.loads(text)

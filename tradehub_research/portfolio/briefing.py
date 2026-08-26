"""Terse deterministic M/W/F exception briefing renderer.

Reads ONLY stored typed rows (observations, transitions, proposals) — never
raw evidence text, thesis text, or tokens.  ``No portfolio action
recommended.`` is a first-class valid output.  Output is byte-deterministic
for identical stored state.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.portfolio.types import C, D, State

BRIEFING_TAG = "portfolio-briefing-v1"
FORMAT_VERSION = "MWF_V1"

# Fixed safe label maps; never renders raw content.
STATE_LABEL = {state.value: state.value for state in State}
ACTION_LABEL = {"BUY": "BUY", "SELL": "SELL"}
CAUSE_LABEL = {
    "RULE_PERSISTED": "rule",
    "MATERIAL_CHANGE": "material-change",
    "VERIFIED_THESIS_BREAK": "verified-break",
    "SETTLEMENT": "settlement",
    "COOLDOWN": "cooldown",
}


def render_briefing(
    *,
    run_id: str,
    decision_as_of: str,
    policy_version: str,
    observations: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
    data_status: list[str],
) -> tuple[str, str]:
    """Render the briefing; returns (body_text, body_hash).

    Inputs must already be ordered deterministically (security_id ascending
    for transitions/proposals; as_of+security ascending for observations).
    """
    lines: list[str] = []
    lines.append("DATA STATUS")
    if data_status:
        for item in data_status:
            lines.append(f"- {item}")
    else:
        lines.append("- OK")
    lines.append("")
    lines.append("PORTFOLIO STATUS")
    lines.append("")

    # Suppressed: unchanged WATCH/HOLD names are not spam (only surfaced when
    # the observation changed or carries a block).
    unchanged = {block["security_id"] for block in blocks}
    surfaced_states: list[str] = []
    for observation in observations:
        security_id = observation["security_id"]
        current = observation["current_state"]
        if hasattr(current, "value"):
            current = current.value
        signal = observation["signal_status"]
        if current in ("WATCH", "HOLD") and signal == "PASS" and security_id not in unchanged:
            continue
        if hasattr(observation["final_status"], "value"):
            final_status = observation["final_status"].value
        else:
            final_status = observation["final_status"]
        if final_status == "NO_ACTION" and current in ("WATCH", "HOLD"):
            continue
        surfaced_states.append(f"- {security_id}: {current} ({signal})")
    if surfaced_states:
        lines.extend(sorted(surfaced_states))
    else:
        lines.append("- No state changes to report.")
    lines.append("")
    lines.append("CHANGES")
    if transitions:
        for transition in transitions:
            lines.append(
                f"- {transition['security_id']}: {transition['from_state']} -> "
                f"{transition['to_state']} "
                f"({CAUSE_LABEL.get(transition['cause'], transition['cause'])})"
            )
    else:
        lines.append("- No state transitions.")
    lines.append("")
    lines.append("PROPOSALS")
    if proposals:
        for proposal in proposals:
            lines.append(
                f"- {proposal['security_id']}: "
                f"{ACTION_LABEL.get(proposal['action'], proposal['action'])} "
                f"{proposal['proposed_state']} target {proposal['target_weight_ppm']}ppm "
                f"notional ${proposal['max_notional_microusd'] // 1_000_000}"
            )
    else:
        lines.append("- No portfolio action recommended.")
    lines.append("")
    lines.append("BLOCKED / NEEDS ATTENTION")
    if blocks:
        for block in blocks:
            lines.append(f"- {block['security_id']}: {block['reason']} ({block['status']})")
    else:
        lines.append("- None")
    body = "\n".join(lines) + "\n"
    body_hash = D(
        BRIEFING_TAG, C({"run_id": run_id, "format_version": FORMAT_VERSION, "body": body})
    )
    return body, body_hash

"""Packet C: deterministic scoring replay.

B4 ("keep current production scoring as comparison variant") and the
confluence on/off ablation call committee/scoring.score_screens() DIRECTLY
-- a pure function with no committee/model state (committee/scoring.py
"Committee/model fields are deliberately absent"). This satisfies the
handoff's tension between §8 B4 (compare current scoring) and §1.3 (no mass
historical LLM committee replay): the deterministic scoring layer is
replayed; committee/model layers are NOT (they are evaluated via telemetry
and forward tracking instead, handoff sec 10).
"""

from __future__ import annotations

from typing import Any

from tradehub_research.committee.scoring import score_screens


def replay_scoring(
    screens: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    scoring_version: int = 2,
) -> dict[str, Any]:
    """Replay the deterministic scoring layer for one grid date.

    screens: screen_result logical dicts (pass/fail/confidence/data_quality
    + raw_features) as produced by the replay path.
    evidence: PIT evidence dicts visible at that as_of.
    Returns the full score_screens() result (base evidence, low-quality,
    missing, staleness, confluence bonus components, and per-security
    conviction projection) -- post-hoc re-aggregation of the returned
    components drives the confluence on/off ablation without modifying
    committee/scoring.py.
    """
    from tradehub_research.committee.store import ScoringSpec

    spec = ScoringSpec(scoring_version=scoring_version).as_dict()
    return score_screens(screens, evidence, spec)


def confluence_bonus_only(result: dict[str, Any]) -> float:
    """Extract the confluence-bonus component of a scoring replay -- used by
    the confluence on/off ablation (ablation 3: current confluence vs
    no-confluence). Returns the aggregate bonus magnitude; the ablation
    compares full-scoring vs full-scoring-minus-this-component."""
    return float(result.get("confluence_bonus", 0.0) or 0.0)

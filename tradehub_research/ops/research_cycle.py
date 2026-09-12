"""M/W/F full research cycle (issue #39 B2).

Deterministic pipeline: data freshness -> eligible universe -> Hunters
(production screening at the last completed US session) -> candidate funnel
-> deterministic scoring -> proposals record. No frontier models are called
on the whole universe; the committee is invoked ONLY where the funnel/scoring
indicates (operator step). No-action is valid.

The cycle writes an append-only cycle log under the research dir and prints
a compact JSON summary (the operator surface consumes it).
"""

from __future__ import annotations

import json
import sys

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB, utc_now
from tradehub_research.funnel import FunnelConfig, run_funnel
from tradehub_research.ops.common import ResearchPaths, last_completed_us_session, research_paths
from tradehub_research.ops.decision_pipeline import (
    persist_ready_scores,
    queue_committee_work,
    run_portfolio_decision,
)
from tradehub_research.screen_store import ScreenStore
from tradehub_research.screening import ScreeningConfig, run_screening
from tradehub_research.validation.experiment_db import ExperimentDB

CYCLE_ALGORITHM = "research-cycle-v1"


def _eligible_universe(research_db: ResearchDB, as_of: str) -> list[str]:
    """Eligible terminal memberships visible at as_of (PIT firewall)."""
    with research_db.connect(read_only=True) as conn:
        rows = conn.execute(
            "WITH RECURSIVE visible_chain(root_id, descendant_id) AS ("
            "  SELECT candidate.id, candidate.id FROM universe_membership candidate "
            "  WHERE candidate.knowledge_time <= ? AND NOT EXISTS ("
            "    SELECT 1 FROM universe_membership predecessor "
            "    WHERE predecessor.id=candidate.supersedes_id AND "
            "    predecessor.knowledge_time <= ?)"
            "  UNION ALL "
            "  SELECT chain.root_id, successor.id FROM visible_chain chain "
            "  JOIN universe_membership successor ON successor.supersedes_id=chain.descendant_id "
            "  WHERE successor.knowledge_time <= ?)"
            "SELECT m.security_id FROM universe_membership m "
            "JOIN visible_chain terminal ON terminal.descendant_id=m.id "
            "WHERE m.eligible=1 AND m.valid_from <= ? AND m.valid_to IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM universe_membership s "
            "  WHERE s.supersedes_id=m.id AND s.knowledge_time <= ?) "
            "ORDER BY m.security_id",
            (as_of, as_of, as_of, as_of, as_of),
        ).fetchall()
    return [str(r[0]) for r in rows]


def run_research_cycle(
    *,
    settings: ResearchSettings,
    experiment_db: ExperimentDB,
    paths: ResearchPaths | None = None,
    as_of=None,
    holdings: frozenset[str] = frozenset(),
) -> dict:
    """Run one deterministic research cycle. Returns the cycle summary."""
    paths = paths or research_paths()
    research_db = ResearchDB(paths.research_db, settings.busy_timeout_ms)
    research_db.migrate()
    as_of_str = (as_of or last_completed_us_session()).isoformat()
    as_of_ts = f"{as_of_str}T20:15:00Z"

    universe = _eligible_universe(research_db, as_of_ts)
    if not universe:
        return {
            "status": "NO_UNIVERSE",
            "as_of": as_of_str,
            "universe_count": 0,
            "reason": "no eligible memberships visible at as_of (PIT firewall)",
        }

    config = ScreeningConfig(holdings=holdings, funnel=FunnelConfig())
    run_id = run_screening(as_of_ts, None, config, database=research_db)

    store = ScreenStore(research_db)
    results = store.load_results_for_funnel(run_id)
    logical_material = store.logical_material(run_id)

    # Convert the store's dict rows into funnel-facing result rows.
    from tradehub_research.funnel import FunnelResultRow

    def _funnel_row(raw: dict) -> FunnelResultRow:
        return FunnelResultRow(
            security_id=str(raw["security_id"]),
            family=str(raw["family"]),
            screen_result_id=str(raw["screen_result_id"]),
            sufficient_data=bool(raw.get("sufficient_data")),
            passed=bool(raw.get("passed")),
            confidence=float(raw.get("confidence") or 0.0),
            data_quality=float(raw.get("data_quality") or 0.0),
            evidence_ids=tuple(json.loads(raw.get("evidence_ids_json") or "[]")),
        )

    funnel_rows = [_funnel_row(r) for r in results]

    # Funnel needs sector + cluster lookups; use the minimal deterministic
    # sources available (sectors from the security table).
    with research_db.connect(read_only=True) as conn:
        sector_rows = conn.execute(
            "SELECT security_id, sector FROM security WHERE sector IS NOT NULL"
        ).fetchall()
    sectors = {str(r[0]): str(r[1]) for r in sector_rows}

    candidates, flags = run_funnel(
        run_id=run_id,
        logical_material=logical_material,
        config=config.funnel,
        universe=universe,
        holdings=set(holdings),
        results=funnel_rows,
        sectors=sectors,
        cluster_lookup={},
    )

    candidate_ids = [c.security_id for c in candidates]
    # Phase 2 owns score persistence.  The old replay-only calculation was a
    # validation helper masquerading as an operational score and left the
    # decision ledger empty.  Queue actual committee work, score only READY
    # committee runs, then call the existing Phase-3 engine.
    committee = queue_committee_work(research_db, run_id)
    scores = persist_ready_scores(research_db, run_id)
    decision = run_portfolio_decision(research_db, pipeline_run_id=run_id, decision_as_of=as_of_ts)

    summary = {
        "status": "OK",
        "as_of": as_of_str,
        "algorithm": CYCLE_ALGORITHM,
        "run_id": run_id,
        "universe_count": len(universe),
        "screens": len(results),
        "candidates": candidate_ids[:50],
        "candidate_count": len(candidate_ids),
        "blocking_flags": flags,
        "committee": committee,
        "score_snapshot_ids": scores["score_snapshots"],
        "committee_pending": scores["committee_pending"],
        "decision": decision,
        "committee_needed": bool(committee["committee_runs"]),
        "created_at": utc_now(),
    }

    paths.research_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.research_dir / "cycle-log.jsonl"
    with log_path.open("a") as handle:
        handle.write(json.dumps(summary, sort_keys=True) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    settings = ResearchSettings()
    experiment_db = ExperimentDB(research_paths().experiment_db)
    summary = run_research_cycle(settings=settings, experiment_db=experiment_db)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary.get("status") in ("OK", "NO_UNIVERSE") else 3


if __name__ == "__main__":
    sys.exit(main())

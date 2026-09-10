"""Production bridge for the persisted research -> PAPER decision path.

This module deliberately does *not* score screen rows itself.  A score is a
Phase-2 artifact and may only be created by :class:`Scorer` after the
committee contract is complete.  The bridge has three boring jobs:

* materialize bounded committee work for funnel candidates;
* turn only READY_TO_SCORE committee runs into immutable score snapshots;
* run the existing portfolio engine with a separately registered, labelled
  provisional PAPER policy and export its already-persisted proposals.

It contains no broker client and no model client.  Missing committee work,
policy, or portfolio state is an explicit, successful no-action outcome.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tradehub_research.committee.pack import EvidencePackBuilder, PackBuildError
from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.committee.scoring import Scorer
from tradehub_research.committee.store import CommitteeStore
from tradehub_research.db import ResearchDB, normalize_ts, utc_now
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.policy import PolicyRegistry, build_policy
from tradehub_research.portfolio.snapshot import build_snapshot
from tradehub_research.portfolio.types import PolicyStatus

PAPER_PROVISIONAL_POLICY_VERSION = "paper-provisional-v1"
DEFAULT_AUTONOMY_INBOX = Path("/var/lib/tradehub/autonomy/proposals")


def _policy_spec() -> dict[str, Any]:
    """Load the owner-authorized, replaceable Phase-3 provisional policy.

    Keeping the contract in a versioned JSON artifact makes every number
    reviewable and prevents a Python fallback from silently becoming doctrine.
    """
    path = Path(__file__).resolve().parents[1] / "policy_specs" / "paper-provisional-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_paper_provisional_policy(database: ResearchDB) -> str:
    """Equality-register the sole operational Phase-3 provisional policy."""
    policy = build_policy(
        PAPER_PROVISIONAL_POLICY_VERSION,
        PolicyStatus.PROVISIONAL,
        _policy_spec(),
    )
    PolicyRegistry(database).register(policy)
    return policy.policy_version


def queue_committee_work(database: ResearchDB, pipeline_run_id: str) -> dict[str, Any]:
    """Create/resume actual committee runs for non-control funnel candidates.

    Work is persisted by the existing router; Hermes obtains it from the
    existing authenticated committee API.  This function never manufactures
    assessments or snapshots.
    """
    store = CommitteeStore(database)
    comparator_hash, scoring_hash = store.ensure_registry_rows()
    router = CommitteeRouter(database)
    queued: list[str] = []
    blocked: list[dict[str, str]] = []
    with database.connect(read_only=True) as conn:
        candidates = conn.execute(
            "SELECT candidate_id FROM candidate WHERE run_id=? AND is_control=0 ORDER BY ordinal",
            (pipeline_run_id,),
        ).fetchall()
    for row in candidates:
        candidate_id = str(row["candidate_id"])
        try:
            pack = EvidencePackBuilder(database).build(candidate_id)
            run_id = store.create_or_resume_committee_run(
                candidate_id=candidate_id,
                pack_hash=pack.pack_hash,
                committee_policy_version=1,
                comparator_config_hash=comparator_hash,
                scoring_config_hash=scoring_hash,
                prompt_versions={"neutral": "v1", "red_team": "v1", "arbiter": "v1"},
                assessment_schema_version=1,
            )
            router.initialize(run_id)
            # status performs deterministic recovery and persists work envelopes.
            router.status(run_id)
            queued.append(run_id)
        except (PackBuildError, ValueError) as exc:
            blocked.append({"candidate_id": candidate_id, "reason": str(exc)})
    return {"committee_runs": queued, "committee_blocked": blocked}


def persist_ready_scores(database: ResearchDB, pipeline_run_id: str) -> dict[str, Any]:
    """Persist only scores which have passed the complete Phase-2 contract."""
    scorer = Scorer(database)
    router = CommitteeRouter(database)
    created: list[str] = []
    pending = 0
    with database.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT committee_run_id FROM committee_run WHERE pipeline_run_id=? ORDER BY committee_run_id",
            (pipeline_run_id,),
        ).fetchall()
    for row in rows:
        run_id = str(row["committee_run_id"])
        status = router.status(run_id)
        if status["state"] != "READY_TO_SCORE":
            pending += 1
            continue
        created.append(str(scorer.create_snapshot(run_id)["snapshot_id"]))
    return {"score_snapshots": created, "committee_pending": pending}


def unknown_portfolio_snapshot(*, decision_as_of: str, pipeline_run_id: str):
    """Represent absent broker state honestly; the engine will fail closed.

    The research plane has no broker credentials and must never infer an empty
    account from missing execution data.  A later execution-side sanitized
    snapshot importer can replace this input without changing portfolio logic.
    """
    return build_snapshot(
        normalize_ts(decision_as_of),
        cash_microusd=None,
        cash_status="UNKNOWN",
        nav_microusd=None,
        valuation_status="UNKNOWN",
        holdings_status="UNKNOWN",
        provenance={
            "kind": "missing_sanitized_broker_snapshot",
            "pipeline_run_id": pipeline_run_id,
            "recorded_by": "decision-pipeline-v1",
        },
        holdings=[],
        market_inputs=[],
    )


def export_eligible_proposals(
    database: ResearchDB, *, run_id: str, inbox: Path | None = None
) -> dict[str, Any]:
    """Export immutable non-fixture proposals as equality-checked envelopes."""
    inbox = inbox or Path(os.environ.get("TRADEHUB_AUTONOMY_INBOX", DEFAULT_AUTONOMY_INBOX))
    exported: list[str] = []
    with database.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT p.*, s.canonical_ticker, ps.as_of AS data_as_of, m.mark_price_microusd, "
            "h.quantity_microunits AS current_quantity_microunits, "
            "h.sellable_quantity_microunits "
            "FROM trade_proposal p "
            "JOIN portfolio_state_observation o ON o.decision_id=p.decision_id "
            "JOIN portfolio_policy pp ON pp.policy_version=p.policy_version "
            "JOIN security s ON s.security_id=p.security_id "
            "JOIN portfolio_snapshot ps ON ps.snapshot_id=p.portfolio_snapshot_id "
            "LEFT JOIN portfolio_market_input m ON m.snapshot_id=p.portfolio_snapshot_id "
            "AND m.security_id=p.security_id "
            "LEFT JOIN portfolio_holding h ON h.snapshot_id=p.portfolio_snapshot_id "
            "AND h.security_id=p.security_id "
            "WHERE o.run_id=? AND pp.policy_status!='FIXTURE' "
            "AND p.proposal_mode='PAPER' ORDER BY p.proposal_id",
            (run_id,),
        ).fetchall()
    inbox.mkdir(parents=True, exist_ok=True)
    for row in rows:
        proposal = dict(row)
        proposal.pop("canonical_ticker", None)
        proposal.pop("data_as_of", None)
        proposal.pop("mark_price_microusd", None)
        proposal.pop("current_quantity_microunits", None)
        proposal.pop("sellable_quantity_microunits", None)
        proposal["mark_price_microusd"] = row["mark_price_microusd"]
        proposal["current_quantity_microunits"] = row["current_quantity_microunits"]
        proposal["sellable_quantity_microunits"] = row["sellable_quantity_microunits"]
        envelope = {
            "schema_version": "paper-proposal-envelope-v1",
            "proposal": proposal,
            "symbol": row["canonical_ticker"],
            "universe": "US_STOCKS",
            "data_as_of": row["data_as_of"],
            "exported_at": utc_now(),
        }
        # Export identity excludes wall-clock observability metadata.
        stable = dict(envelope)
        stable.pop("exported_at")
        body = json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n"
        path = inbox / f"{row['proposal_id']}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing.pop("exported_at", None)
            if existing != stable:
                raise ValueError(f"proposal export collision for {row['proposal_id']}")
        else:
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)
        exported.append(str(row["proposal_id"]))
    return {"eligible_exports": exported}


def run_portfolio_decision(
    database: ResearchDB,
    *,
    pipeline_run_id: str,
    decision_as_of: str,
    inbox: Path | None = None,
    snapshot=None,
) -> dict[str, Any]:
    """Run the existing engine when genuine persisted scores are available."""
    try:
        policy_version = ensure_paper_provisional_policy(database)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "BLOCKED_NO_POLICY",
            "reason": f"provisional policy unavailable: {type(exc).__name__}",
            "portfolio_run": None,
            "eligible_exports": [],
        }
    with database.connect(read_only=True) as conn:
        score_count = conn.execute(
            "SELECT count(*) FROM score_snapshot s JOIN committee_run c "
            "ON c.committee_run_id=s.committee_run_id WHERE c.pipeline_run_id=?",
            (pipeline_run_id,),
        ).fetchone()[0]
    if not score_count:
        return {"status": "BLOCKED_NO_VALID_SCORE", "portfolio_run": None, "eligible_exports": []}
    missing_portfolio_state = snapshot is None
    snapshot = snapshot or unknown_portfolio_snapshot(
        decision_as_of=decision_as_of, pipeline_run_id=pipeline_run_id
    )
    summary = PortfolioEngine(database).run(
        pipeline_run_id=pipeline_run_id,
        policy_version=policy_version,
        snapshot=snapshot,
        decision_as_of=decision_as_of,
        allow_provisional=True,
        allow_fixture=False,
    )
    exported = export_eligible_proposals(database, run_id=summary.run_id, inbox=inbox)
    return {
        "status": (
            "BLOCKED_MISSING_PORTFOLIO_STATE"
            if missing_portfolio_state
            else ("HEALTHY_ZERO_ACTION" if summary.proposal_count == 0 else "PROPOSALS_EXPORTED")
        ),
        "portfolio_run": summary.as_dict(),
        **exported,
    }

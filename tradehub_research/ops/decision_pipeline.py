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
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
DEFAULT_PORTFOLIO_HANDOFF = Path("/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.json")
DEFAULT_PORTFOLIO_HANDOFF_HISTORY = Path(
    "/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.jsonl"
)


class PortfolioStateUnavailable(ValueError):
    """The execution-owned portfolio handoff cannot prove a usable PAPER state."""


def _microusd(value: Any, field: str) -> int:
    """Strict dollars -> micro-USD conversion; floats never silently round."""
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PortfolioStateUnavailable(f"{field} is not a decimal amount") from exc
    if not decimal.is_finite() or decimal < 0:
        raise PortfolioStateUnavailable(f"{field} must be a finite non-negative amount")
    return int((decimal * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def _load_handoff_at_or_before(
    *,
    decision_as_of: str,
    path: Path | None,
) -> dict[str, Any]:
    """Choose the latest sanitized handoff knowable at decision_as_of."""
    if path is not None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PortfolioStateUnavailable(
                f"sanitized portfolio handoff unavailable: {type(exc).__name__}"
            ) from exc
        return payload
    try:
        decision_time = datetime.fromisoformat(normalize_ts(decision_as_of).replace("Z", "+00:00"))
        candidates = []
        for line in DEFAULT_PORTFOLIO_HANDOFF_HISTORY.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            observed = datetime.fromisoformat(str(item.get("as_of", "")).replace("Z", "+00:00"))
            if observed <= decision_time:
                candidates.append((observed, item))
    except (OSError, ValueError) as exc:
        raise PortfolioStateUnavailable(
            f"sanitized portfolio handoff history unavailable: {type(exc).__name__}"
        ) from exc
    if not candidates:
        raise PortfolioStateUnavailable("no sanitized portfolio handoff precedes decision_as_of")
    return max(candidates, key=lambda pair: pair[0])[1]


def load_sanitized_paper_snapshot(
    *,
    decision_as_of: str,
    pipeline_run_id: str,
    path: Path | None = None,
):
    """Load the execution-owned, credential-free PAPER portfolio handoff.

    Research never calls the broker or reads execution credentials. A missing,
    stale, malformed, non-PAPER, or nonempty-unmapped handoff is fail-closed.
    An explicitly empty broker positions list is a known empty book and is a
    valid no-action portfolio snapshot.
    """
    payload = _load_handoff_at_or_before(decision_as_of=decision_as_of, path=path)
    if not isinstance(payload, dict):
        raise PortfolioStateUnavailable("sanitized portfolio handoff is not an object")
    if payload.get("account_type") != "PAPER":
        raise PortfolioStateUnavailable(
            "sanitized portfolio handoff does not prove PAPER account type"
        )
    if payload.get("account_status") not in {"Open", "Funded", "New"}:
        raise PortfolioStateUnavailable("sanitized portfolio handoff account status is unusable")
    handoff_as_of = str(payload.get("as_of", ""))
    try:
        handoff_time = datetime.fromisoformat(handoff_as_of.replace("Z", "+00:00"))
        decision_time = datetime.fromisoformat(normalize_ts(decision_as_of).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioStateUnavailable("sanitized portfolio handoff as_of is invalid") from exc
    if handoff_time > decision_time:
        raise PortfolioStateUnavailable("sanitized portfolio handoff is after decision_as_of")
    # One market-session / weekend buffer.  The runner separately rejects
    # stale market data; the portfolio state is deliberately conservative.
    if (decision_time - handoff_time).total_seconds() > 78 * 3600:
        raise PortfolioStateUnavailable("sanitized portfolio handoff is stale")
    positions = payload.get("positions")
    if not isinstance(positions, list):
        raise PortfolioStateUnavailable("sanitized portfolio positions are missing")
    if positions:
        # Symbol->security mapping and trustworthy position valuation are not
        # yet handed off. Never interpret a nonempty book as empty.
        raise PortfolioStateUnavailable("nonempty broker positions lack a typed research mapping")
    return build_snapshot(
        normalize_ts(decision_as_of),
        cash_microusd=_microusd(payload.get("cash_balance"), "cash_balance"),
        cash_status="KNOWN",
        nav_microusd=_microusd(payload.get("asset_value"), "asset_value"),
        valuation_status="KNOWN",
        holdings_status="KNOWN",
        provenance={
            "kind": "execution_sanitized_paper_handoff_v1",
            "pipeline_run_id": pipeline_run_id,
            "handoff_as_of": handoff_as_of,
            "handoff_path": str(path),
        },
        holdings=[],
        market_inputs=[],
    )


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
            "SELECT committee_run_id FROM committee_run WHERE pipeline_run_id=? "
            "ORDER BY committee_run_id",
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
        total = conn.execute(
            "SELECT count(*) FROM trade_proposal p "
            "JOIN portfolio_state_observation o ON o.decision_id=p.decision_id "
            "WHERE o.run_id=?",
            (run_id,),
        ).fetchone()[0]
        blocked_human_approval = conn.execute(
            "SELECT count(*) FROM trade_proposal p "
            "JOIN portfolio_state_observation o ON o.decision_id=p.decision_id "
            "WHERE o.run_id=? AND p.requires_human_approval=1",
            (run_id,),
        ).fetchone()[0]
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
            "AND p.proposal_mode='PAPER' AND p.requires_human_approval=0 "
            "ORDER BY p.proposal_id",
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
    return {
        "proposal_count": total,
        "blocked_human_approval": blocked_human_approval,
        "eligible_exports": exported,
    }


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
    try:
        snapshot = snapshot or load_sanitized_paper_snapshot(
            decision_as_of=decision_as_of,
            pipeline_run_id=pipeline_run_id,
        )
    except PortfolioStateUnavailable as exc:
        return {
            "status": "BLOCKED_MISSING_PORTFOLIO_STATE",
            "reason": str(exc),
            "portfolio_run": None,
            "eligible_exports": [],
        }
    summary = PortfolioEngine(database).run(
        pipeline_run_id=pipeline_run_id,
        policy_version=policy_version,
        snapshot=snapshot,
        decision_as_of=decision_as_of,
        allow_provisional=True,
        allow_fixture=False,
    )
    exported = export_eligible_proposals(database, run_id=summary.run_id, inbox=inbox)
    if exported["eligible_exports"]:
        status = "PROPOSALS_EXPORTED"
    elif exported["blocked_human_approval"]:
        status = "BLOCKED_REQUIRES_HUMAN_APPROVAL"
    else:
        status = "HEALTHY_ZERO_ACTION"
    return {
        "status": status,
        "portfolio_run": summary.as_dict(),
        **exported,
    }


def finalize_async_committee_decisions(
    database: ResearchDB,
    *,
    inbox: Path | None = None,
    handoff: Path | None = None,
) -> dict[str, Any]:
    """Advance asynchronous committee work without calling a model.

    Committee workers submit independently through the existing API. This
    finalizer periodically detects only their persisted READY_TO_SCORE result,
    creates immutable score snapshots, then runs the existing portfolio engine
    and proposal exporter. Any invalid/missing state becomes an explicit
    blocked result; it never manufactures an assessment, score, proposal, or
    executable order.
    """
    # Do not revisit every historical pending committee run on every timer
    # tick.  A pipeline is eligible for continuation only when it has a run
    # whose latest durable state is READY_TO_SCORE, or when a score has
    # already been written.  The latter is important: a crash (or a missing
    # execution-side handoff) after scoring must be recoverable on a later
    # tick without creating a second score or stranding the original
    # pipeline_run_id.
    with database.connect(read_only=True) as conn:
        runs = conn.execute(
            "WITH latest_state AS ("
            " SELECT committee_run_id, to_state, "
            " ROW_NUMBER() OVER (PARTITION BY committee_run_id ORDER BY rowid DESC) AS ordinal "
            " FROM committee_transition"
            ") "
            "SELECT DISTINCT c.pipeline_run_id FROM committee_run c "
            "LEFT JOIN latest_state state ON state.committee_run_id=c.committee_run_id "
            " AND state.ordinal=1 "
            "LEFT JOIN score_snapshot score ON score.committee_run_id=c.committee_run_id "
            "WHERE state.to_state='READY_TO_SCORE' OR score.snapshot_id IS NOT NULL "
            "ORDER BY c.pipeline_run_id"
        ).fetchall()
    results: list[dict[str, Any]] = []
    for row in runs:
        pipeline_run_id = str(row["pipeline_run_id"])
        scores = persist_ready_scores(database, pipeline_run_id)
        # create_snapshot is idempotent, but only reports a snapshot while a
        # run is READY_TO_SCORE.  Count durable snapshots separately so a
        # retry resumes a previously scored original pipeline run.
        with database.connect(read_only=True) as conn:
            persisted_score_count = conn.execute(
                "SELECT count(*) FROM score_snapshot s "
                "JOIN committee_run c ON c.committee_run_id=s.committee_run_id "
                "WHERE c.pipeline_run_id=?",
                (pipeline_run_id,),
            ).fetchone()[0]
        if not persisted_score_count:
            results.append(
                {
                    "pipeline_run_id": pipeline_run_id,
                    "status": "PENDING_COMMITTEE",
                    **scores,
                    "eligible_exports": [],
                }
            )
            continue
        with database.connect(read_only=True) as conn:
            run_row = conn.execute(
                "SELECT as_of FROM pipeline_run WHERE run_id=?", (pipeline_run_id,)
            ).fetchone()
        if run_row is None:
            results.append(
                {
                    "pipeline_run_id": pipeline_run_id,
                    "status": "BLOCKED_MISSING_PIPELINE_RUN",
                    **scores,
                    "eligible_exports": [],
                }
            )
            continue
        try:
            decision = run_portfolio_decision(
                database,
                pipeline_run_id=pipeline_run_id,
                decision_as_of=str(run_row["as_of"]),
                inbox=inbox,
                snapshot=(
                    load_sanitized_paper_snapshot(
                        decision_as_of=str(run_row["as_of"]),
                        pipeline_run_id=pipeline_run_id,
                        path=handoff,
                    )
                    if handoff is not None
                    else None
                ),
            )
        except (ValueError, OSError) as exc:
            decision = {
                "status": "BLOCKED_FINALIZER_ERROR",
                "reason": f"{type(exc).__name__}: {exc}",
                "eligible_exports": [],
            }
        results.append({"pipeline_run_id": pipeline_run_id, **scores, **decision})
    return {"status": "OK", "finalized": results, "created_at": utc_now()}


__all__ = [
    "ensure_paper_provisional_policy",
    "export_eligible_proposals",
    "finalize_async_committee_decisions",
    "load_sanitized_paper_snapshot",
    "persist_ready_scores",
    "queue_committee_work",
    "run_portfolio_decision",
]

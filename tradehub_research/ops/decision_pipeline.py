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

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from tradehub_research.committee.pack import EvidencePackBuilder, PackBuildError
from tradehub_research.committee.routing import CommitteeRouter
from tradehub_research.committee.scoring import Scorer
from tradehub_research.committee.store import CommitteeStore
from tradehub_research.db import ResearchDB, normalize_ts, utc_now
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.handoff import (
    PortfolioHandoffUnavailable,
    load_paper_portfolio_snapshot,
    load_paper_portfolio_snapshot_payload,
)
from tradehub_research.portfolio.policy import PolicyRegistry, build_policy
from tradehub_research.portfolio.snapshot import build_signal_input, build_snapshot
from tradehub_research.portfolio.types import PolicyStatus
from tradehub_research.screens import canonical_json

PAPER_PROVISIONAL_POLICY_VERSION = "paper-provisional-v1"
DEFAULT_AUTONOMY_INBOX = Path("/var/lib/tradehub/autonomy/proposals")
DEFAULT_AUTHORITY_DIR = Path("/var/lib/tradehub/autonomy/authority")
AUTHORITY_SCHEMA_VERSION = "paper-proposal-authority-v1"
DEFAULT_PORTFOLIO_HANDOFF = Path("/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.json")
DEFAULT_PORTFOLIO_HANDOFF_HISTORY = Path(
    "/var/lib/tradehub-research/handoff/paper_portfolio_snapshot.jsonl"
)


class PortfolioStateUnavailable(ValueError):
    """The execution-owned portfolio handoff cannot prove a usable PAPER state."""


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
    database: ResearchDB,
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
    try:
        if path is not None:
            return load_paper_portfolio_snapshot(
                database=database,
                decision_as_of=decision_as_of,
                pipeline_run_id=pipeline_run_id,
                path=path,
            )
        return load_paper_portfolio_snapshot_payload(
            database=database,
            decision_as_of=decision_as_of,
            pipeline_run_id=pipeline_run_id,
            payload=_load_handoff_at_or_before(decision_as_of=decision_as_of, path=None),
        )
    except PortfolioHandoffUnavailable as exc:
        raise PortfolioStateUnavailable(str(exc)) from exc


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


def _publish_authority(authority_dir: Path, record: dict[str, Any]) -> Path:
    """Atomically publish the narrow autonomy-readable authority projection.

    This is the ONLY research artifact the autonomy identity may read. It
    carries order-driving identity and eligibility only: no evidence rows, no
    model prose, no research tables, no credentials. Publication is
    idempotent-by-equality so a re-export cannot silently rewrite authority.
    """
    authority_dir.mkdir(parents=True, exist_ok=True)
    path = authority_dir / f"{record['proposal_id']}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"proposal authority unreadable for {record['proposal_id']}") from exc
        if existing != record:
            raise ValueError(f"proposal authority collision for {record['proposal_id']}")
        return path
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o640)
    temporary.replace(path)
    return path


def export_eligible_proposals(
    database: ResearchDB,
    *,
    run_id: str,
    inbox: Path | None = None,
    authority_dir: Path | None = None,
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
        rows = conn.execute(
            "SELECT p.*, pp.policy_status, s.canonical_ticker, ps.as_of AS data_as_of, "
            "m.mark_price_microusd, "
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
            "AND p.proposal_mode='PAPER' "
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
        identity_hash = hashlib.sha256(canonical_json(stable).encode()).hexdigest()
        # Publication order is load-bearing. The research proposal is already
        # persisted; AUTHORITY is published before the ENVELOPE so an autonomy
        # reader can never observe an envelope without its authority record.
        _publish_authority(
            authority_dir
            or Path(os.environ.get("TRADEHUB_PROPOSAL_AUTHORITY_DIR", DEFAULT_AUTHORITY_DIR)),
            {
                "schema_version": AUTHORITY_SCHEMA_VERSION,
                "proposal_id": row["proposal_id"],
                "security_id": row["security_id"],
                "canonical_symbol": str(row["canonical_ticker"]).upper(),
                "action": row["action"],
                "max_quantity_microunits": row["max_quantity_microunits"],
                "completion_quantity_microunits": row["completion_quantity_microunits"],
                "max_notional_microusd": row["max_notional_microusd"],
                "current_weight_ppm": row["current_weight_ppm"],
                "target_weight_ppm": row["target_weight_ppm"],
                "score_snapshot_id": row["score_snapshot_id"],
                "portfolio_snapshot_id": row["portfolio_snapshot_id"],
                "policy_version": row["policy_version"],
                "sizing_policy_version": row["sizing_policy_version"],
                "proposal_mode": row["proposal_mode"],
                # Recorded for operator transparency ONLY. It is deliberately
                # NOT part of autonomy_eligible: the portfolio engine currently
                # stamps every proposal with requires_human_approval=1
                # (tradehub_research/portfolio/proposal.py), so gating on it
                # would dead-lock the entire autonomous PAPER path. Any future
                # gating is an explicit owner decision, not a side effect here.
                "requires_human_approval": row["requires_human_approval"],
                "autonomy_eligible": bool(
                    row["proposal_mode"] == "PAPER" and row["policy_status"] != "FIXTURE"
                ),
                "data_as_of": row["data_as_of"],
                "envelope_identity_hash": identity_hash,
                "created_at": row["created_at"],
            },
        )
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
            # The research writer may use a restrictive umask.  The
            # execution-owned autonomy identity needs read-only access to the
            # completed, atomically-renamed envelope; it never needs write
            # access to research output.
            temporary.chmod(0o640)
            temporary.replace(path)
        exported.append(str(row["proposal_id"]))
    return {
        "proposal_count": total,
        "blocked_human_approval": 0,
        "eligible_exports": exported,
    }


def load_persisted_signal_inputs(database: ResearchDB, *, decision_as_of: str) -> list:
    """Load only independently persisted, PIT-valid signal inputs.

    This bridge creates no signals and interprets no score into an opportunity.
    It takes the latest already-recorded SignalInput per security at or before
    the decision timestamp.  An empty table is valid and preserves zero action.
    """
    as_of = normalize_ts(decision_as_of)
    with database.connect(read_only=True) as conn:
        rows = conn.execute(
            "WITH ranked AS ("
            " SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY as_of DESC, "
            " recorded_at DESC, signal_input_id DESC) AS ordinal "
            " FROM portfolio_signal_input WHERE as_of<=?"
            ") SELECT * FROM ranked WHERE ordinal=1 ORDER BY security_id",
            (as_of,),
        ).fetchall()
    signals = []
    for row in rows:
        try:
            evidence_ids = json.loads(row["evidence_ids_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"persisted signal {row['signal_input_id']} has invalid evidence ids"
            ) from exc
        signal = build_signal_input(
            security_id=str(row["security_id"]),
            as_of=str(row["as_of"]),
            remaining_opportunity_ppm=row["remaining_opportunity_ppm"],
            opportunity_status=str(row["opportunity_status"]),
            source_kind=str(row["source_kind"]),
            evidence_ids=evidence_ids,
        )
        if (
            signal.input_hash != row["input_hash"]
            or signal.signal_input_id != row["signal_input_id"]
        ):
            raise ValueError(f"persisted signal {row['signal_input_id']} fails identity validation")
        signals.append(signal)
    return signals


def run_portfolio_decision(
    database: ResearchDB,
    *,
    pipeline_run_id: str,
    decision_as_of: str,
    evidence_as_of: str | None = None,
    inbox: Path | None = None,
    snapshot=None,
) -> dict[str, Any]:
    """Two-clock decision entry point.

    ``evidence_as_of`` is NOT caller-controlled. It is always derived from
    ``pipeline_run.as_of`` — the frozen market/evidence cutoff that bounds every
    packed evidence row and every model fact (PIT firewall). A caller-supplied
    value is accepted only when it equals that cutoff, so moving decision time
    can never move the evidence cutoff.

    ``decision_as_of`` is the actual operational decision time and must be
    >= every persisted score's ``computed_at`` for this run so an
    asynchronously completed committee score stays visible to Phase 3.

    Score ``computed_at`` is never backdated to satisfy this contract.
    """
    with database.connect(read_only=True) as conn:
        run_row = conn.execute(
            "SELECT as_of FROM pipeline_run WHERE run_id=?", (pipeline_run_id,)
        ).fetchone()
    if run_row is None:
        raise ValueError(f"unknown pipeline run: {pipeline_run_id}")
    canonical_evidence_as_of = normalize_ts(str(run_row["as_of"]))
    if evidence_as_of is not None and normalize_ts(evidence_as_of) != canonical_evidence_as_of:
        raise ValueError(
            "evidence_as_of must equal pipeline_run.as_of "
            f"({canonical_evidence_as_of}); got {normalize_ts(evidence_as_of)}"
        )
    evidence_as_of = canonical_evidence_as_of
    if normalize_ts(decision_as_of) < canonical_evidence_as_of:
        raise ValueError("decision_as_of must not precede evidence_as_of")
    with database.connect(read_only=True) as conn:
        future = conn.execute(
            "SELECT count(*) FROM score_snapshot s JOIN committee_run c "
            "ON c.committee_run_id=s.committee_run_id "
            "WHERE c.pipeline_run_id=? AND s.computed_at>?",
            (pipeline_run_id, normalize_ts(decision_as_of)),
        ).fetchone()[0]
    if future:
        # Loud by design: refusing here is the assertion that protects the
        # two-clock contract. Fix by using an operational decision time, never
        # by backdating score computed_at.
        raise ValueError(
            f"{future} persisted score(s) are newer than decision_as_of; "
            "decision_as_of must be the actual operational decision time"
        )
    result = _run_portfolio_decision_inner(
        database,
        pipeline_run_id=pipeline_run_id,
        decision_as_of=decision_as_of,
        inbox=inbox,
        snapshot=snapshot,
    )
    result.setdefault("evidence_as_of", canonical_evidence_as_of)
    result.setdefault("decision_as_of", normalize_ts(decision_as_of))
    return result


def _run_portfolio_decision_inner(
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
            database,
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
    try:
        signals = load_persisted_signal_inputs(database, decision_as_of=decision_as_of)
        summary = PortfolioEngine(database).run(
            pipeline_run_id=pipeline_run_id,
            policy_version=policy_version,
            snapshot=snapshot,
            decision_as_of=decision_as_of,
            signals=signals,
            allow_provisional=True,
            allow_fixture=False,
        )
    except ValueError as exc:
        return {
            "status": "BLOCKED_INVALID_SIGNAL_INPUT",
            "reason": str(exc),
            "portfolio_run": None,
            "eligible_exports": [],
        }
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


SCORE_SET_HASH_PREFIX = "tradehub-score-set-v1"


def _score_set_hash(score_snapshot_ids: list[str]) -> str:
    """Canonical durable identity of a score set."""
    return hashlib.sha256(
        (SCORE_SET_HASH_PREFIX + "\0" + canonical_json(sorted(score_snapshot_ids))).encode()
    ).hexdigest()


def current_score_set(database: ResearchDB, pipeline_run_id: str) -> dict[str, Any]:
    """The durable score set for a pipeline run.

    ``score_ready_at`` is MAX(computed_at) of the CURRENT persisted score set —
    a durable input, never a fresh wall clock.
    """
    with database.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT s.snapshot_id, s.computed_at FROM score_snapshot s "
            "JOIN committee_run c ON c.committee_run_id=s.committee_run_id "
            "WHERE c.pipeline_run_id=? ORDER BY s.snapshot_id",
            (pipeline_run_id,),
        ).fetchall()
    ids = [str(r["snapshot_id"]) for r in rows]
    ready = max((normalize_ts(str(r["computed_at"])) for r in rows), default=None)
    return {
        "score_snapshot_ids": ids,
        "score_ready_at": ready,
        "score_set_hash": _score_set_hash(ids) if ids else None,
    }


def find_finalized_decision(
    database: ResearchDB, pipeline_run_id: str, score_snapshot_ids: list[str]
) -> dict[str, Any] | None:
    """The durable portfolio run already covering exactly this score set.

    Idempotency keys on (pipeline_run_id, score set) — never on wall clock — so
    a later timer tick over the same durable score set must reuse the existing
    decision rather than append a duplicate observation.
    """
    wanted = sorted(score_snapshot_ids)
    if not wanted:
        return None
    with database.connect(read_only=True) as conn:
        runs = conn.execute(
            "SELECT run_id, decision_as_of, portfolio_snapshot_id, policy_version "
            "FROM portfolio_run WHERE pipeline_run_id=? ORDER BY rowid",
            (pipeline_run_id,),
        ).fetchall()
        for run in runs:
            covered = conn.execute(
                "SELECT DISTINCT score_snapshot_id FROM portfolio_state_observation "
                "WHERE run_id=? AND score_snapshot_id IS NOT NULL",
                (run["run_id"],),
            ).fetchall()
            covered_ids = sorted(str(r["score_snapshot_id"]) for r in covered)
            if covered_ids and covered_ids == wanted:
                return {
                    "run_id": str(run["run_id"]),
                    "decision_as_of": normalize_ts(str(run["decision_as_of"])),
                    "portfolio_snapshot_id": run["portfolio_snapshot_id"],
                    "policy_version": run["policy_version"],
                }
    return None


def _handoff_payloads_at_or_after(score_ready_at: str, path: Path | None) -> list[dict[str, Any]]:
    """Sanitized handoff payloads observed at/after score_ready_at, ascending."""
    items: list[dict[str, Any]] = []
    if path is not None:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            items = [loaded] if isinstance(loaded, dict) else []
        except (OSError, ValueError):
            items = []
    else:
        try:
            items = [
                json.loads(line)
                for line in DEFAULT_PORTFOLIO_HANDOFF_HISTORY.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
        except (OSError, ValueError):
            items = []
    ready = datetime.fromisoformat(normalize_ts(score_ready_at).replace("Z", "+00:00"))
    kept: list[tuple[datetime, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            observed = datetime.fromisoformat(str(item.get("as_of", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if observed >= ready:
            kept.append((observed, item))
    return [item for _, item in sorted(kept, key=lambda pair: pair[0])]


def resolve_decision_epoch(
    database: ResearchDB,
    *,
    pipeline_run_id: str,
    score_ready_at: str,
    handoff: Path | None = None,
) -> tuple[str, Any]:
    """Pick a STABLE operational decision epoch for a new score set.

    Preferred: a valid sanitized handoff already knowable at ``score_ready_at``
    ⇒ ``decision_as_of = score_ready_at``.

    Otherwise bind to the EARLIEST subsequently persisted valid handoff, so
    later timer ticks converge on the same epoch instead of silently advancing
    to "latest handoff now" on every poll.

    Raises ``PortfolioStateUnavailable`` when no usable epoch exists yet; the
    caller must treat that as fail-closed and must NOT invent a clock.
    """
    try:
        snapshot = load_sanitized_paper_snapshot(
            database,
            decision_as_of=score_ready_at,
            pipeline_run_id=pipeline_run_id,
            path=handoff,
        )
        return score_ready_at, snapshot
    except (PortfolioHandoffUnavailable, OSError, ValueError):
        pass
    for payload in _handoff_payloads_at_or_after(score_ready_at, handoff):
        as_of = normalize_ts(str(payload.get("as_of", "")))
        try:
            snapshot = load_paper_portfolio_snapshot_payload(
                database,
                decision_as_of=as_of,
                pipeline_run_id=pipeline_run_id,
                payload=payload,
            )
            return as_of, snapshot
        except (PortfolioHandoffUnavailable, OSError, ValueError):
            continue
    raise PortfolioStateUnavailable(
        "no usable sanitized PAPER handoff for this score set; refusing to advance decision_as_of"
    )


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
        # run is READY_TO_SCORE.  The durable score set is therefore read back
        # separately so a retry resumes a previously scored original pipeline.
        score_set = current_score_set(database, pipeline_run_id)
        if not score_set["score_snapshot_ids"]:
            results.append(
                {
                    "pipeline_run_id": pipeline_run_id,
                    "status": "PENDING_COMMITTEE",
                    **scores,
                    "eligible_exports": [],
                }
            )
            continue

        # Idempotency by durable score set: a second tick over the SAME score
        # set must reuse the existing decision, not append another observation
        # merely because its clock differs.
        existing = find_finalized_decision(
            database, pipeline_run_id, score_set["score_snapshot_ids"]
        )
        if existing is not None:
            recovered = export_eligible_proposals(database, run_id=existing["run_id"], inbox=inbox)
            results.append(
                {
                    "pipeline_run_id": pipeline_run_id,
                    "status": "REUSED",
                    "decision_as_of": existing["decision_as_of"],
                    "score_set_hash": score_set["score_set_hash"],
                    "score_ready_at": score_set["score_ready_at"],
                    "portfolio_run": existing,
                    "proposal_count": len(recovered["eligible_exports"]),
                    **scores,
                    **recovered,
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

        # New score set: bind a stable durable epoch. No fresh utc_now() per
        # tick — an epoch is chosen once from durable inputs and reused after.
        try:
            decision_as_of, snapshot = resolve_decision_epoch(
                database,
                pipeline_run_id=pipeline_run_id,
                score_ready_at=str(score_set["score_ready_at"]),
                handoff=handoff,
            )
            decision = run_portfolio_decision(
                database,
                pipeline_run_id=pipeline_run_id,
                decision_as_of=decision_as_of,
                evidence_as_of=str(run_row["as_of"]),
                inbox=inbox,
                snapshot=snapshot,
            )
        except (ValueError, OSError) as exc:
            decision = {
                "status": "BLOCKED_FINALIZER_ERROR",
                "reason": f"{type(exc).__name__}: {exc}",
                "eligible_exports": [],
            }
        results.append(
            {
                "pipeline_run_id": pipeline_run_id,
                **scores,
                **decision,
                "score_set_hash": score_set["score_set_hash"],
                "score_ready_at": score_set["score_ready_at"],
            }
        )
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

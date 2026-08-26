"""Portfolio engine: deterministic orchestration of one decision run.

Pipeline: score snapshot -> eligibility -> persistence/hysteresis -> risk ->
sizing -> proposal -> budget -> briefing.  One ``BEGIN IMMEDIATE`` transaction
per run; identical invocation returns the stored run (idempotent replay) and
never writes twice.  TradeHub code — never an LLM — decides state transitions.

Write discipline: everything is computed in memory first; proposals and their
transitions are written ONLY after budget admission (a budget-rejected draft
leaves no transition and no proposal), then observations, then the briefing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts
from tradehub_research.portfolio import budget as budget_module
from tradehub_research.portfolio import prices
from tradehub_research.portfolio.briefing import FORMAT_VERSION, render_briefing
from tradehub_research.portfolio.budget import Budget
from tradehub_research.portfolio.eligibility import evaluate_eligibility, latest_verified_break
from tradehub_research.portfolio.policy import PolicyRegistry, PolicySpec
from tradehub_research.portfolio.proposal import build_proposal
from tradehub_research.portfolio.risk import KNOWN as KNOWN_STATUS
from tradehub_research.portfolio.risk import RiskEngine, RiskInputs
from tradehub_research.portfolio.sizing import size_buy, size_sell
from tradehub_research.portfolio.snapshot import (
    PortfolioSnapshot,
    SignalInput,
    SnapshotStore,
)
from tradehub_research.portfolio.state import (
    TRANSITION_CAUSES,
    cooldown_satisfied,
    current_state,
    pending_resolution,
    persistence_count,
)
from tradehub_research.portfolio.types import (
    PENDING_STATES,
    TRIGGER_TO_REASON,
    Action,
    C,
    D,
    FinalStatus,
    PolicyStatus,
    RiskStatus,
    SignalStatus,
    State,
    json_roundtrip,
)

RUN_TAG = "portfolio-run-v1"
INVOCATION_TAG = "portfolio-invocation-v1"
DECISION_TAG = "portfolio-decision-v1"
TRANSITION_TAG = "portfolio-transition-v1"
BRIEFING_TAG = "portfolio-briefing-v1"

PENDING_REASON = "pending_unsettled"

# Edges that carry an actionable BUY/SELL paper proposal.
ACTIONABLE_EDGES = frozenset(
    {
        (State.WATCH, State.ENTER),
        (State.HOLD, State.ADD),
        (State.HOLD, State.TRIM),
        (State.HOLD, State.EXIT),
        (State.TRIM, State.EXIT),
    }
)


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    invocation_key: str
    status: str
    observation_count: int
    transition_count: int
    proposal_count: int
    briefing: str
    briefing_hash: str
    reused: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "invocation_key": self.invocation_key,
            "status": self.status,
            "observation_count": self.observation_count,
            "transition_count": self.transition_count,
            "proposal_count": self.proposal_count,
            "briefing": self.briefing,
            "briefing_hash": self.briefing_hash,
            "reused": self.reused,
        }


class PortfolioEngine:
    def __init__(self, database: ResearchDB):
        self.database = database
        self.policies = PolicyRegistry(database)
        self.snapshots = SnapshotStore(database)

    # ------------------------------------------------------------------
    # run entry
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        pipeline_run_id: str,
        policy_version: str,
        snapshot: PortfolioSnapshot,
        decision_as_of: str,
        signals: list[SignalInput] | None = None,
        allow_provisional: bool = False,
        allow_fixture: bool = False,
    ) -> RunSummary:
        """Execute one deterministic portfolio decision run (single transaction)."""
        as_of = normalize_ts(decision_as_of)
        created_at = as_of
        policy = self.policies.get(policy_version)
        if policy.policy_status == PolicyStatus.FIXTURE and not allow_fixture:
            raise ValueError(f"policy {policy_version!r} is FIXTURE; not acceptable for this run")
        if policy.policy_status == PolicyStatus.PROVISIONAL and not allow_provisional:
            raise ValueError(f"policy {policy_version!r} is PROVISIONAL; pass --allow-provisional")
        # PIT discipline: no input may be dated after the decision moment.
        if snapshot.as_of > as_of:
            raise ValueError(f"snapshot as_of {snapshot.as_of!r} is after decision_as_of {as_of!r}")
        signals = signals or []
        signal_by_id: dict[str, SignalInput] = {}
        for signal in signals:
            if signal.as_of > as_of:
                raise ValueError(
                    f"signal {signal.security_id!r} as_of {signal.as_of!r} is after "
                    f"decision_as_of {as_of!r}"
                )
            if signal.security_id in signal_by_id:
                raise ValueError(
                    f"more than one signal input for security {signal.security_id!r}; "
                    "exactly one signal per security is required"
                )
            signal_by_id[signal.security_id] = signal
        signal_mapping_hash = C(
            sorted((f"{sid}:{signal.signal_input_id}" for sid, signal in signal_by_id.items()))
        )

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")

            # Resolve the decision-driving world state BEFORE any idempotency
            # lookup: scores, state head, observations, verified breaks,
            # market evidence, and budget prestate all bind the invocation.
            score_rows = self._pinned_scores(db, pipeline_run_id, as_of)
            score_by_security: dict[str, dict[str, Any]] = {}
            for row in score_rows:
                security_id = row["security_id"]
                if security_id in score_by_security:
                    raise ValueError(
                        f"multiple pinned score snapshots for security {security_id!r} in "
                        f"pipeline run {pipeline_run_id!r}; exactly one terminal score per "
                        "security is required (fail closed)"
                    )
                score_by_security[security_id] = row
            candidate_ids = self._candidate_set(db, snapshot, score_by_security, as_of)
            self._require_known_securities(db, candidate_ids)
            self._reconcile_holding_valuations(policy, snapshot)

            activity_date = as_of[:10]
            state_prestate_hash = self._state_prestate_hash(db, candidate_ids, as_of)
            observation_prestate_hash = self._observation_prestate_hash(
                db, candidate_ids, policy, as_of
            )
            thesis_prestate_hash = self._thesis_prestate_hash(db, candidate_ids, as_of)
            market_prestate_hash = self._market_prestate_hash(db, candidate_ids, as_of)
            budget_prestate = self._budget_prestate(db, activity_date, policy, as_of)
            budget_prestate_hash = C(budget_prestate)

            score_ids = sorted({row["snapshot_id"] for row in score_rows})
            score_set_hash = C(score_ids)
            candidate_set_hash = C(sorted(candidate_ids))
            invocation_key = D(
                INVOCATION_TAG,
                C(
                    {
                        "pipeline_run_id": pipeline_run_id,
                        "policy_version": policy_version,
                        "snapshot_id": snapshot.snapshot_id,
                        "signal_mapping_hash": signal_mapping_hash,
                        "score_set_hash": score_set_hash,
                        "state_prestate_hash": state_prestate_hash,
                        "observation_prestate_hash": observation_prestate_hash,
                        "thesis_prestate_hash": thesis_prestate_hash,
                        "market_data_prestate_hash": market_prestate_hash,
                        "budget_prestate_hash": budget_prestate_hash,
                        "decision_as_of": as_of,
                    }
                ),
            )
            existing_run = db.execute(
                "SELECT * FROM portfolio_run WHERE invocation_key=?", (invocation_key,)
            ).fetchone()
            if existing_run is not None:
                stored_input_hash = existing_run["input_hash"]
                input_hash = C(
                    {
                        "invocation_key": invocation_key,
                        "state_prestate_hash": state_prestate_hash,
                        "observation_prestate_hash": observation_prestate_hash,
                        "thesis_prestate_hash": thesis_prestate_hash,
                        "market_data_prestate_hash": market_prestate_hash,
                        "budget_prestate_hash": budget_prestate_hash,
                        "policy_spec_hash": policy.spec_hash,
                        "score_set_hash": score_set_hash,
                        "signal_mapping_hash": signal_mapping_hash,
                        "candidate_set_hash": candidate_set_hash,
                    }
                )
                if stored_input_hash != input_hash:
                    raise ValueError(
                        "portfolio run exists for this invocation but its input hash differs; "
                        "world state changed under the same invocation — use a new decision_as_of"
                    )
                briefing = db.execute(
                    "SELECT body_text,body_hash FROM portfolio_briefing WHERE run_id=?",
                    (existing_run["run_id"],),
                ).fetchone()
                observation_count = db.execute(
                    "SELECT count(*) FROM portfolio_state_observation WHERE run_id=?",
                    (existing_run["run_id"],),
                ).fetchone()[0]
                transition_count = db.execute(
                    "SELECT count(*) FROM portfolio_state_transition WHERE decision_id IN "
                    "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
                    (existing_run["run_id"],),
                ).fetchone()[0]
                proposal_count = db.execute(
                    "SELECT count(*) FROM trade_proposal WHERE decision_id IN "
                    "(SELECT decision_id FROM portfolio_state_observation WHERE run_id=?)",
                    (existing_run["run_id"],),
                ).fetchone()[0]
                return RunSummary(
                    run_id=existing_run["run_id"],
                    invocation_key=invocation_key,
                    status="REUSED",
                    observation_count=observation_count,
                    transition_count=transition_count,
                    proposal_count=proposal_count,
                    briefing=briefing["body_text"] if briefing else "",
                    briefing_hash=briefing["body_hash"] if briefing else "",
                    reused=True,
                )

            # Equality-insert immutable inputs inside this transaction.
            self.snapshots.save_snapshot(snapshot, recorded_at=created_at, db=db)
            for signal in signals:
                self.snapshots.save_signal_input(signal, recorded_at=created_at, db=db)

            input_hash = C(
                {
                    "invocation_key": invocation_key,
                    "state_prestate_hash": state_prestate_hash,
                    "observation_prestate_hash": observation_prestate_hash,
                    "thesis_prestate_hash": thesis_prestate_hash,
                    "market_data_prestate_hash": market_prestate_hash,
                    "budget_prestate_hash": budget_prestate_hash,
                    "policy_spec_hash": policy.spec_hash,
                    "score_set_hash": score_set_hash,
                    "signal_mapping_hash": signal_mapping_hash,
                    "candidate_set_hash": candidate_set_hash,
                }
            )
            run_id = D(RUN_TAG, input_hash)
            db.execute(
                "INSERT INTO portfolio_run("
                "run_id,pipeline_run_id,decision_as_of,portfolio_snapshot_id,policy_version,"
                "score_set_hash,signal_set_hash,candidate_set_hash,invocation_key,"
                "state_prestate_hash,market_data_prestate_hash,budget_prestate_hash,input_hash,"
                "expected_security_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    pipeline_run_id,
                    as_of,
                    snapshot.snapshot_id,
                    policy.policy_version,
                    score_set_hash,
                    signal_mapping_hash,
                    candidate_set_hash,
                    invocation_key,
                    state_prestate_hash,
                    market_prestate_hash,
                    budget_prestate_hash,
                    input_hash,
                    len(candidate_ids),
                    created_at,
                ),
            )

            # --- per-security decisions (in memory; nothing written yet) ----
            risk_engine = RiskEngine(db, policy, snapshot)
            budget_state = Budget(self.database).bind_day(
                activity_date,
                policy,
                created_at=created_at,
                db=db,
                day_start_cash_microusd=snapshot.cash_microusd,
            )
            decisions: list[dict[str, Any]] = []
            for security_id in sorted(candidate_ids):
                decisions.append(
                    self._decide(
                        db=db,
                        run_id=run_id,
                        security_id=security_id,
                        as_of=as_of,
                        created_at=created_at,
                        policy=policy,
                        snapshot=snapshot,
                        score=score_by_security.get(security_id),
                        signal=signal_by_id.get(security_id),
                        risk_engine=risk_engine,
                        activity_date=activity_date,
                    )
                )

            # --- budget admission ------------------------------------------
            drafts = [draft for decision in decisions for draft in decision["drafts"]]
            admitted_drafts, rejected = budget_module.admit_drafts(
                budget_state,
                drafts,
                policy,
            )
            admitted_by_security = {draft["security_id"]: draft for draft in admitted_drafts}

            # --- writes (FK order: observations -> transitions -> proposals) --
            # Phase A: resolve budget-admission outcome on each decision.
            for decision in decisions:
                security_id = decision["security_id"]
                if not decision["drafts"]:
                    continue
                if admitted_by_security.get(security_id) is not None:
                    decision["final_status"] = FinalStatus.PROPOSED
                else:
                    reason = rejected.get(security_id, "daily_budget_exhausted")
                    decision["final_status"] = FinalStatus.BLOCKED
                    decision["reason_codes"] = sorted(set(decision["reason_codes"] + [reason]))
                    decision["blocks"].append(
                        {
                            "security_id": security_id,
                            "state": decision["current_state"].value,
                            "reason": reason,
                        }
                    )

            # Phase B: observations.
            observations = [self._observation_row(decision) for decision in decisions]
            for observation in observations:
                db.execute(
                    "INSERT INTO portfolio_state_observation("
                    "decision_id,run_id,security_id,current_state,signal_state,proposed_state,"
                    "score_snapshot_id,signal_input_id,portfolio_snapshot_id,policy_version,"
                    "scored_evidence_hash,change_cause,evidence_driven,signal_status,"
                    "persistence_count_at_decision,persistence_required,material_change_satisfied,"
                    "cooldown_satisfied,risk_status,final_status,reason_codes_json,risk_json,"
                    "sizing_json,decision_input_hash,observed_at,recorded_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    observation,
                )

            # Phase C: transitions and admitted proposals.
            written_transitions: list[dict[str, Any]] = []
            for decision in decisions:
                security_id = decision["security_id"]
                if decision["drafts"]:
                    draft = admitted_by_security.get(security_id)
                    if draft is not None:
                        self._write_transition(db, decision["pending_transition"])
                        self._write_proposal(db, draft)
                        written_transitions.append(decision["pending_transition"])
                else:
                    for transition in decision["transitions"]:
                        self._write_transition(db, transition)
                        written_transitions.append(transition)

            # --- briefing ----------------------------------------------------
            proposal_rows = self._stored_proposals(db, run_id)
            briefing_body, briefing_hash = render_briefing(
                run_id=run_id,
                decision_as_of=as_of,
                policy_version=policy.policy_version,
                observations=decisions,
                transitions=sorted(
                    written_transitions, key=lambda t: (t["security_id"], t["effective_at"])
                ),
                proposals=proposal_rows,
                blocks=[block for decision in decisions for block in decision["blocks"]],
                data_status=self._data_status(snapshot, policy),
            )
            db.execute(
                "INSERT INTO portfolio_briefing("
                "briefing_id,run_id,format_version,body_text,body_hash,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    D(
                        BRIEFING_TAG,
                        C(
                            {
                                "run_id": run_id,
                                "format_version": FORMAT_VERSION,
                                "body_hash": briefing_hash,
                            }
                        ),
                    ),
                    run_id,
                    FORMAT_VERSION,
                    briefing_body,
                    briefing_hash,
                    created_at,
                ),
            )
            if len(observations) != len(candidate_ids):
                raise AssertionError(
                    f"expected {len(candidate_ids)} observations, wrote {len(observations)}"
                )

        return RunSummary(
            run_id=run_id,
            invocation_key=invocation_key,
            status="COMPLETE",
            observation_count=len(observations),
            transition_count=len(written_transitions),
            proposal_count=len(proposal_rows),
            briefing=briefing_body,
            briefing_hash=briefing_hash,
            reused=False,
        )

    # ------------------------------------------------------------------
    # input helpers
    # ------------------------------------------------------------------

    def _pinned_scores(self, db: Any, pipeline_run_id: str, as_of: str) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT s.snapshot_id,s.candidate_id,c.security_id,s.conviction,s.data_quality,"
            "s.committee_agreement,s.trajectory_label,s.change_cause,s.material_change_time,"
            "s.prior_conviction,s.conviction_delta,s.scored_evidence_hash,s.score_input_hash,"
            "s.reason_codes_json,s.scoring_config_hash,s.computed_at "
            "FROM score_snapshot s JOIN committee_run r ON r.committee_run_id=s.committee_run_id "
            "JOIN candidate c ON c.candidate_id=s.candidate_id "
            "WHERE r.pipeline_run_id=? AND s.computed_at<=? "
            "ORDER BY c.security_id,s.computed_at DESC,s.material_change_time DESC,s.snapshot_id",
            (pipeline_run_id, as_of),
        ).fetchall()
        return [dict(row) for row in rows]

    def _reconcile_holding_valuations(
        self, policy: PolicySpec, snapshot: PortfolioSnapshot
    ) -> None:
        """Quantity x mark must reconcile with the claimed holding market value.

        A snapshot whose market value understates quantity x mark bypasses
        concentration caps and can manufacture ADD proposals.  Divergence
        beyond ``snapshot_tolerance_ppm`` blocks the run (fail closed).
        """
        tolerance = int(policy.risk.get("snapshot_tolerance_ppm", 0))
        for holding in snapshot.holdings:
            security_id = holding["security_id"]
            quantity = holding.get("quantity_microunits")
            market_value = holding.get("market_value_microusd")
            market_input = snapshot.market_input(security_id)
            mark = market_input.get("mark_price_microusd") if market_input else None
            if (
                quantity is None
                or market_value is None
                or mark is None
                or holding.get("valuation_status", "KNOWN") != "KNOWN"
                or market_input.get("price_status", "KNOWN") != "KNOWN"
            ):
                continue
            implied = quantity * mark // 1_000_000
            if implied == 0:
                continue
            divergence = abs(market_value - implied) * 1_000_000 // implied
            if divergence > tolerance:
                raise ValueError(
                    f"holding {security_id} market value {market_value} does not reconcile "
                    f"with quantity x mark {implied} (divergence {divergence} ppm > "
                    f"snapshot_tolerance_ppm {tolerance})"
                )

    def _candidate_set(
        self,
        db: Any,
        snapshot: PortfolioSnapshot,
        score_by_security: dict[str, Any],
        as_of: str,
    ) -> set[str]:
        candidates: set[str] = set(score_by_security.keys())
        for holding in snapshot.holdings:
            candidates.add(holding["security_id"])
        for row in db.execute(
            "SELECT DISTINCT security_id FROM portfolio_state_transition WHERE effective_at<=?",
            (as_of,),
        ).fetchall():
            state = current_state(db, row["security_id"], as_of)
            if state["state"] != State.DISCOVER:
                candidates.add(row["security_id"])
        return candidates

    def _require_known_securities(self, db: Any, candidate_ids: set[str]) -> None:
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = db.execute(
            f"SELECT security_id FROM security WHERE security_id IN ({placeholders})",
            sorted(candidate_ids),
        ).fetchall()
        known = {row["security_id"] for row in rows}
        missing = sorted(candidate_ids - known)
        if missing:
            raise ValueError(f"candidate securities missing from security table: {missing}")

    def _state_prestate_hash(self, db: Any, candidate_ids: set[str], as_of: str) -> str:
        """State head STRICTLY BEFORE as_of.

        The run's own writes carry ``effective_at == as_of``; strict-before
        semantics keep the prestate byte-identical across an idempotent rerun
        while still capturing every prior transition.
        """
        pinned: list[str] = []
        for security_id in sorted(candidate_ids):
            head = db.execute(
                "SELECT transition_id FROM portfolio_state_transition "
                "WHERE security_id=? AND effective_at<? "
                "ORDER BY effective_at DESC,created_at DESC LIMIT 1",
                (security_id, as_of),
            ).fetchone()
            if head is not None:
                pinned.append(f"{security_id}:{head['transition_id']}")
        return C(pinned)

    def _observation_prestate_hash(
        self, db: Any, candidate_ids: set[str], policy: PolicySpec, as_of: str
    ) -> str:
        """Persistence-driving observation history STRICTLY BEFORE as_of.

        The current run's hypothetical observation (at ``as_of``) is excluded;
        it is fully determined by inputs already bound into the invocation.
        """
        pinned: list[str] = []
        for security_id in sorted(candidate_ids):
            rows = db.execute(
                "SELECT decision_id,observed_at,scored_evidence_hash,signal_status,signal_state "
                "FROM portfolio_state_observation "
                "WHERE security_id=? AND policy_version=? AND evidence_driven=1 AND observed_at<? "
                "ORDER BY observed_at,decision_id",
                (security_id, policy.policy_version, as_of),
            ).fetchall()
            pinned.extend(
                f"{security_id}:{row['decision_id']}:{row['observed_at']}:"
                f"{row['scored_evidence_hash']}:{row['signal_status']}:{row['signal_state']}"
                for row in rows
            )
        return C(pinned)

    def _thesis_prestate_hash(self, db: Any, candidate_ids: set[str], as_of: str) -> str:
        """Verified thesis-break rows are part of the run identity."""
        pinned: list[str] = []
        for security_id in sorted(candidate_ids):
            rows = db.execute(
                "SELECT verification_id,status,verified_at FROM thesis_break_verification "
                "WHERE event_id IN (SELECT event_id FROM thesis_break_event WHERE security_id=?) "
                "AND verified_at<=? ORDER BY verified_at,verification_id",
                (security_id, as_of),
            ).fetchall()
            pinned.extend(
                f"{security_id}:{row['verification_id']}:{row['status']}:{row['verified_at']}"
                for row in rows
            )
        return C(pinned)

    def _market_prestate_hash(self, db: Any, candidate_ids: set[str], as_of: str) -> str:
        ids: list[str] = []
        for security_id in sorted(candidate_ids):
            records = prices._visible_records(db, security_id, as_of)
            ids.extend(
                record["evidence_id"]
                for record in records
                if record["structured_fields"].get("record_type")
                in ("price_bar", "split", "dividend")
            )
        return C(sorted(set(ids)))

    def _budget_prestate(
        self, db: Any, activity_date: str, policy: PolicySpec, as_of: str
    ) -> dict[str, Any]:
        prior = db.execute(
            "SELECT proposal_id,max_notional_microusd FROM trade_proposal "
            "WHERE activity_date=? AND created_at<?",
            (activity_date, as_of),
        ).fetchall()
        return {
            "activity_date": activity_date,
            "policy_version": policy.policy_version,
            "prior_proposal_ids": sorted(row["proposal_id"] for row in prior),
            "prior_count": len(prior),
            "prior_notional_microusd": sum(int(row["max_notional_microusd"]) for row in prior),
        }

    # ------------------------------------------------------------------
    # decision core
    # ------------------------------------------------------------------

    def _decide(
        self,
        *,
        db: Any,
        run_id: str,
        security_id: str,
        as_of: str,
        created_at: str,
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        score: dict[str, Any] | None,
        signal: SignalInput | None,
        risk_engine: RiskEngine,
        activity_date: str,
    ) -> dict[str, Any]:
        holding = snapshot.holding(security_id)
        current = current_state(db, security_id, as_of)
        current_state_value = current["state"]
        trusted_quantity = holding["quantity_microunits"] if holding else None
        position_present = bool(holding and holding["quantity_microunits"] > 0)

        decision: dict[str, Any] = {
            "run_id": run_id,
            "security_id": security_id,
            "as_of": as_of,
            "created_at": created_at,
            "current_state": current_state_value,
            "portfolio_snapshot_id": snapshot.snapshot_id,
            "policy_version": policy.policy_version,
            "score": score,
            "signal": signal,
            "holding": holding,
            "signal_state": current_state_value.value,
            "proposed_state": current_state_value.value,
            "signal_status": SignalStatus.UNKNOWN.value,
            "final_status": FinalStatus.NO_ACTION,
            "persistence_count": 0,
            "persistence_required": 0,
            "material_change_satisfied": 0,
            "cooldown_satisfied": 1,
            "risk_status": RiskStatus.NOT_RUN.value,
            "reason_codes": [],
            "risk_json": {},
            "sizing_json": {},
            "transitions": [],
            "pending_transition": None,
            "drafts": [],
            "blocks": [],
            "score_snapshot_id": score["snapshot_id"] if score else None,
            "signal_input_id": signal.signal_input_id if signal else None,
            "scored_evidence_hash": score.get("scored_evidence_hash") if score else None,
            "change_cause": score.get("change_cause") if score else None,
            "evidence_driven": int(
                score is not None and score.get("change_cause") == "EVIDENCE_DRIVEN"
            ),
        }
        decision_id = D(
            DECISION_TAG,
            C(
                {
                    "run_id": run_id,
                    "security_id": security_id,
                    "score_snapshot_id": decision["score_snapshot_id"],
                    "signal_input_id": decision["signal_input_id"],
                }
            ),
        )
        decision["decision_id"] = decision_id

        # --- pass 1: pending-state settlement -----------------------------
        if current_state_value in PENDING_STATES:
            outcome, satisfied, reason = pending_resolution(
                db,
                security_id,
                current,
                trusted_quantity,
                as_of,
                int(policy.settlement["quantity_tolerance_microunits"]),
                int(policy.settlement["pending_max_calendar_days"]),
                quantity_status=(
                    holding.get("quantity_status", KNOWN_STATUS) if holding else KNOWN_STATUS
                ),
            )
            if outcome != "STILL_PENDING" and outcome != "NOT_PENDING":
                self._settle(
                    decision,
                    db,
                    policy,
                    snapshot,
                    security_id,
                    current,
                    outcome,
                    reason,
                    as_of,
                    created_at,
                    score,
                )
                return decision
            decision["reason_codes"] = [PENDING_REASON]
            decision["signal_status"] = SignalStatus.INELIGIBLE.value
            decision["final_status"] = FinalStatus.NO_ACTION
            return decision

        # --- pass 2: eligibility ------------------------------------------
        context = self._eligibility_context(
            decision,
            db,
            policy,
            snapshot,
            security_id,
            current_state_value,
            position_present,
            score,
            signal,
            as_of,
        )
        eligibility = evaluate_eligibility(policy, current_state_value, context)
        decision["signal_status"] = eligibility.status
        decision["signal_state"] = (
            eligibility.to_state.value if eligibility.to_state else current_state_value.value
        )
        if eligibility.status != "PASS":
            decision["final_status"] = (
                FinalStatus.BLOCKED if eligibility.status == "BLOCKED" else FinalStatus.NO_ACTION
            )
            if eligibility.opportunity_blocked:
                decision["reason_codes"] = ["opportunity_unknown"]
            elif eligibility.status == "BLOCKED":
                decision["reason_codes"] = [eligibility.reason_code or "policy_ineligible"]
            if eligibility.status == "BLOCKED":
                decision["blocks"].append(
                    {
                        "security_id": security_id,
                        "state": current_state_value.value,
                        "reason": decision["reason_codes"][0],
                    }
                )
            return decision

        edge = (current_state_value, eligibility.to_state)
        assert eligibility.to_state is not None
        reason_code = eligibility.reason_code or TRIGGER_TO_REASON.get(
            eligibility.trigger_kind, "score_band"
        )
        decision["proposed_state"] = eligibility.to_state.value
        decision["reason_codes"] = [reason_code]

        cooldown_days = policy.cooldown_days(*edge)
        cooldown_ok = cooldown_satisfied(current["effective_at"], as_of, cooldown_days)
        decision["cooldown_satisfied"] = int(cooldown_ok)

        transition_ok = False
        cause: str | None = None
        persistence_required = policy.persistence_required(*edge)
        decision["persistence_required"] = persistence_required

        if eligibility.trigger_kind == "VERIFIED_THESIS_BREAK":
            verification = context.get("verified_break")
            if verification is not None and policy.allows_verified_break_bypass(*edge):
                transition_ok = True
                cause = "VERIFIED_THESIS_BREAK"
                decision["thesis_verification_id"] = verification["verification_id"]
                if decision.get("score_snapshot_id") is None:
                    # a verified break acts even without a current score: the
                    # transition lineage falls back to the break's own
                    # detection score snapshot (schema requires lineage)
                    decision["score_snapshot_id"] = verification.get("score_snapshot_id")

        # Every other trigger (SCORE_BAND, RISK_REDUCTION, DATA_INTEGRITY,
        # POLICY_INELIGIBLE, THESIS_REALISED, OPPORTUNITY_COST) must satisfy
        # cooldown AND evidence persistence — only the verified-break bypass
        # is exempt.  This centralizes hysteresis: no trigger kind may skip
        # persistence, and no reason is dead configuration.
        if not transition_ok:
            if not cooldown_ok:
                decision["final_status"] = FinalStatus.NO_ACTION
                decision["reason_codes"] = ["cooldown_active"]
                return decision
            persistence = persistence_count(
                db,
                security_id,
                policy.policy_version,
                as_of,
                current_state_value,
                eligibility.to_state,
                decision_id,
                decision["scored_evidence_hash"],
                decision["signal_status"],
                eligibility.to_state,
                hypothetical_evidence_driven=bool(decision.get("evidence_driven")),
            )
            decision["persistence_count"] = persistence
            if persistence >= persistence_required:
                transition_ok = True
                cause = "RULE_PERSISTED"
            else:
                material = self._material_change_satisfied(
                    policy, edge, score, current["effective_at"]
                )
                decision["material_change_satisfied"] = int(material)
                if material and policy.allows_material_bypass(*edge):
                    transition_ok = True
                    cause = "MATERIAL_CHANGE"

        if not transition_ok:
            decision["final_status"] = FinalStatus.NO_ACTION
            return decision

        is_actionable = edge in ACTIONABLE_EDGES
        if not is_actionable:
            decision["transitions"].append(
                self._transition_row(
                    decision,
                    policy,
                    snapshot,
                    security_id,
                    current_state_value,
                    eligibility.to_state,
                    cause,
                    as_of,
                    created_at,
                )
            )
            decision["final_status"] = FinalStatus.TRANSITIONED
            decision["risk_status"] = RiskStatus.NOT_RUN.value
            return decision

        # SELL asymmetry: a score-band decline is not a sell reason.
        if eligibility.to_state in (State.TRIM, State.EXIT) and reason_code == "score_band":
            decision["final_status"] = FinalStatus.NO_ACTION
            decision["reason_codes"] = ["no_sell_reason"]
            return decision

        risk_inputs = self._risk_inputs(
            db,
            decision,
            snapshot,
            security_id,
            current_state_value,
            position_present,
            eligibility.to_state,
        )
        risk_result = risk_engine.evaluate(risk_inputs, as_of)
        decision["risk_status"] = risk_result.status
        decision["risk_json"] = risk_result.as_dict()
        if risk_result.status in ("BLOCKED", "UNKNOWN"):
            decision["final_status"] = FinalStatus.BLOCKED
            decision["reason_codes"] = sorted(set(decision["reason_codes"] + risk_result.reasons))
            decision["blocks"].append(
                {
                    "security_id": security_id,
                    "status": risk_result.status,
                    "reason": ",".join(sorted(risk_result.reasons)) or "risk_blocked",
                }
            )
            return decision

        sizing_result = self._size(
            policy,
            snapshot,
            decision,
            current_state_value,
            eligibility.to_state,
            risk_result,
            trusted_quantity,
        )
        decision["sizing_json"] = sizing_result.as_dict()
        if sizing_result.action is None:
            decision["final_status"] = FinalStatus.NO_ACTION
            decision["reason_codes"] = [sizing_result.reason]
            return decision

        # The effective proposed state may differ from the eligibility state:
        # an infeasible full EXIT degrades to TRIM (sizing carries the truth).
        effective_state = sizing_result.effective_state or eligibility.to_state
        transition = self._transition_row(
            decision,
            policy,
            snapshot,
            security_id,
            current_state_value,
            effective_state,
            cause,
            as_of,
            created_at,
        )
        decision["pending_transition"] = transition
        draft = self._draft(
            decision,
            transition,
            policy,
            snapshot,
            security_id,
            effective_state,
            sizing_result,
            reason_code,
            activity_date,
            created_at,
        )
        decision["drafts"] = [draft]
        decision["final_status"] = FinalStatus.PROPOSED
        return decision

    # ------------------------------------------------------------------
    # sub-steps
    # ------------------------------------------------------------------

    def _eligibility_context(
        self,
        decision: dict[str, Any],
        db: Any,
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        security_id: str,
        current_state_value: State,
        position_present: bool,
        score: dict[str, Any] | None,
        signal: SignalInput | None,
        as_of: str,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "conviction_ppm": round(score["conviction"] * 10_000) if score else None,
            "data_quality_ppm": round(score["data_quality"] * 1_000_000) if score else None,
            "agreement_ppm": round(score["committee_agreement"] * 1_000_000)
            if score and score.get("committee_agreement") is not None
            else None,
            "trajectory": score.get("trajectory_label") if score else None,
            "position_present": position_present,
        }
        opportunity = signal.remaining_opportunity_ppm if signal else None
        context["opportunity_ppm"] = opportunity
        context["opportunity_known"] = bool(signal and opportunity is not None)
        security = db.execute(
            "SELECT sector,sector_coverage_status,delisted_at FROM security WHERE security_id=?",
            (security_id,),
        ).fetchone()
        context["sector_coverage_status"] = security["sector_coverage_status"] if security else None
        if security and security["delisted_at"] is not None and security["delisted_at"] <= as_of:
            context["policy_ineligible"] = True
        realised_max = int(policy.thesis_break.get("realised_opportunity_max_ppm", 0))
        if opportunity is not None and opportunity <= realised_max:
            context["thesis_realised"] = True
        opportunity_cost_max = int(policy.thesis_break.get("opportunity_cost_max_ppm", 0))
        if opportunity is not None and opportunity <= opportunity_cost_max:
            context["opportunity_cost_trigger"] = True
        verified = latest_verified_break(
            db,
            security_id,
            as_of,
            list(policy.thesis_break["allowed_verification_methods"]),
            int(policy.thesis_break["max_age_calendar_days"]),
        )
        if verified is not None:
            context["verified_break_eligible"] = True
            context["verified_break"] = verified
        market_input = snapshot.market_input(security_id)
        if (
            position_present
            and market_input is not None
            and market_input.get("price_status")
            in (
                "STALE",
                "UNKNOWN",
            )
        ):
            context["data_integrity_failure"] = True
        holding = snapshot.holding(security_id)
        if holding is not None and snapshot.nav_microusd is not None:
            weight_ppm = round(holding["market_value_microusd"] * 1_000_000 / snapshot.nav_microusd)
            if weight_ppm > int(policy.risk["max_position_ppm"]):
                context["risk_reduction_trigger"] = True
            sector = holding.get("sector")
            if sector is not None and snapshot.sector_total_ppm(sector) > int(
                policy.risk["max_sector_ppm"]
            ):
                context["risk_reduction_trigger"] = True
        return context

    def _risk_inputs(
        self,
        db: Any,
        decision: dict[str, Any],
        snapshot: PortfolioSnapshot,
        security_id: str,
        current_state_value: State,
        position_present: bool,
        proposed_state: State,
    ) -> RiskInputs:
        if proposed_state in (State.ENTER, State.ADD):
            direction = Action.BUY
        elif proposed_state in (State.TRIM, State.EXIT):
            direction = Action.SELL
        else:
            direction = None
        holding = snapshot.holding(security_id)
        market_input = snapshot.market_input(security_id)
        sector = holding.get("sector") if holding else None
        if sector is None:
            row = db.execute(
                "SELECT sector FROM security WHERE security_id=?", (security_id,)
            ).fetchone()
            sector = row["sector"] if row else None
        current_weight_ppm = 0
        if (
            holding is not None
            and holding.get("market_value_microusd") is not None
            and snapshot.nav_microusd
        ):
            current_weight_ppm = round(
                holding["market_value_microusd"] * 1_000_000 / snapshot.nav_microusd
            )
        return RiskInputs(
            security_id=security_id,
            sector=sector,
            sector_coverage_status=None,
            current_state=current_state_value,
            position_present=position_present,
            trusted_quantity_microunits=holding.get("quantity_microunits") if holding else None,
            quantity_status=holding.get("quantity_status", KNOWN_STATUS)
            if holding
            else KNOWN_STATUS,
            sellable_quantity_microunits=holding.get("sellable_quantity_microunits")
            if holding
            else None,
            sellable_status=holding.get("sellable_status", KNOWN_STATUS)
            if holding
            else KNOWN_STATUS,
            mark_price_microusd=market_input.get("mark_price_microusd") if market_input else None,
            price_status=market_input.get("price_status", "UNKNOWN") if market_input else "UNKNOWN",
            price_as_of=market_input.get("price_as_of") if market_input else None,
            adv_microusd=market_input.get("avg_dollar_volume_microusd") if market_input else None,
            liquidity_status=market_input.get("liquidity_status", "UNKNOWN")
            if market_input
            else "UNKNOWN",
            liquidity_as_of=market_input.get("liquidity_as_of") if market_input else None,
            nav_microusd=snapshot.nav_microusd,
            nav_status=snapshot.valuation_status.value,
            cash_microusd=snapshot.cash_microusd,
            cash_status=snapshot.cash_status.value,
            holdings_status=snapshot.holdings_status.value,
            holding_valuation_status=snapshot.valuation_status.value,
            current_weight_ppm=current_weight_ppm,
            direction=direction,
        )

    def _material_change_satisfied(
        self,
        policy: PolicySpec,
        edge: tuple[State, State],
        score: dict[str, Any] | None,
        state_entry_effective_at: str | None,
    ) -> bool:
        if score is None:
            return False
        if score.get("change_cause") != "EVIDENCE_DRIVEN":
            return False
        edge_key = f"{edge[0].value}_{edge[1].value}"
        direction = policy.material_change["direction_by_edge"].get(edge_key, "NONE")
        delta = score.get("conviction_delta")
        if delta is None:
            return False
        # conviction_delta is stored in 0-100 score points (Phase 2 scale);
        # the policy threshold is expressed in ppm.  Convert before comparing.
        delta_ppm = int(delta) * 10_000
        threshold = int(policy.material_change["conviction_delta_ppm"])
        if direction == "UP" and delta_ppm >= threshold:
            pass
        elif direction == "DOWN" and delta_ppm <= -threshold:
            pass
        else:
            return False
        material_time = score.get("material_change_time")
        if material_time is None or state_entry_effective_at is None:
            return False
        return material_time > state_entry_effective_at

    def _size(
        self,
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        decision: dict[str, Any],
        current_state_value: State,
        proposed_state: State,
        risk_result: Any,
        trusted_quantity: int | None,
    ) -> Any:
        score = decision["score"]
        holding = snapshot.holding(decision["security_id"])
        market_input = snapshot.market_input(decision["security_id"])
        nav = snapshot.nav_microusd
        mark = market_input.get("mark_price_microusd") if market_input else None
        current_weight_ppm = 0
        current_quantity = 0
        if holding is not None:
            current_quantity = holding.get("quantity_microunits", 0)
            if holding.get("market_value_microusd") is not None and nav:
                current_weight_ppm = round(holding["market_value_microusd"] * 1_000_000 / nav)
        if nav is None or mark is None:
            raise AssertionError("risk gate must guarantee nav/mark before sizing")
        increment = int(policy.order_constraints["quantity_increment_microunits"])
        min_notional = int(policy.sizing["min_action_notional_microusd"])
        if proposed_state in (State.ENTER, State.ADD):
            return size_buy(
                policy,
                conviction_ppm=round(score["conviction"] * 10_000),
                data_quality_ppm=round(score["data_quality"] * 1_000_000),
                agreement_ppm=round(score["committee_agreement"] * 1_000_000)
                if score.get("committee_agreement") is not None
                else 0,
                trajectory=score["trajectory_label"],
                current_weight_ppm=current_weight_ppm,
                nav_microusd=nav,
                mark_price_microusd=mark,
                quantity_increment_microunits=increment,
                clips=risk_result.clips,
                available_cash_microusd=snapshot.cash_microusd or 0,
                current_quantity_microunits=current_quantity,
                min_action_notional_microusd=min_notional,
            )
        full_exit = proposed_state == State.EXIT
        sellable = holding.get("sellable_quantity_microunits") if holding else 0
        adv = market_input.get("avg_dollar_volume_microusd") if market_input else None
        return size_sell(
            policy,
            current_weight_ppm=current_weight_ppm,
            current_quantity_microunits=current_quantity,
            sellable_quantity_microunits=sellable or 0,
            mark_price_microusd=mark,
            nav_microusd=nav,
            quantity_increment_microunits=increment,
            full_exit=full_exit,
            min_action_notional_microusd=min_notional,
            adv_microusd=adv,
            max_adv_participation_ppm=int(policy.risk["max_adv_participation_ppm"]),
        )

    def _settle(
        self,
        decision: dict[str, Any],
        db: Any,
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        security_id: str,
        current: dict[str, Any],
        outcome: str,
        reason: str | None,
        as_of: str,
        created_at: str,
        score: dict[str, Any] | None,
    ) -> None:
        state = current["state"]
        if outcome == "TRIM_EXIT_CANDIDATE":
            exit_ok, exit_cause = self._trim_exit_authorized(
                decision, db, policy, snapshot, security_id, as_of
            )
            if exit_ok:
                target = State.EXIT
                cause = exit_cause
            else:
                target = State.HOLD
                cause = "SETTLEMENT"
        elif outcome == "SETTLE_HOLD":
            target = State.HOLD
            cause = "SETTLEMENT"
        elif outcome == "SETTLE_WATCH":
            target = State.WATCH
            cause = "SETTLEMENT"
        else:
            raise ValueError(f"unexpected settlement outcome {outcome}")
        # The settlement continues the originating decision lineage.
        decision["settlement_score_snapshot_id"] = self._settlement_score_id(
            db, security_id, current, decision
        )
        decision["signal_state"] = target.value
        decision["proposed_state"] = target.value
        decision["cooldown_satisfied"] = 1
        decision["reason_codes"] = [reason or "settled"]
        decision["transitions"].append(
            self._transition_row(
                decision,
                policy,
                snapshot,
                security_id,
                state,
                target,
                cause,
                as_of,
                created_at,
            )
        )
        decision["final_status"] = FinalStatus.TRANSITIONED
        decision["signal_status"] = SignalStatus.INELIGIBLE.value

    def _settlement_score_id(
        self,
        db: Any,
        security_id: str,
        current: dict[str, Any],
        decision: dict[str, Any],
    ) -> str:
        # Settlement continues the ORIGINATING proposal's lineage: the score
        # that authorized the pending action, never a newer unrelated score.
        if current["decision_id"] is not None:
            row = db.execute(
                "SELECT score_snapshot_id FROM trade_proposal WHERE decision_id=?",
                (current["decision_id"],),
            ).fetchone()
            if row is not None:
                return row["score_snapshot_id"]
        if decision.get("score_snapshot_id") is not None:
            return decision["score_snapshot_id"]
        row = db.execute(
            "SELECT snapshot_id FROM score_snapshot s JOIN candidate c "
            "ON c.candidate_id=s.candidate_id "
            "WHERE c.security_id=? ORDER BY s.computed_at DESC LIMIT 1",
            (security_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"cannot settle {security_id}: no score snapshot lineage available")
        return row["snapshot_id"]

    def _trim_exit_authorized(
        self,
        decision: dict[str, Any],
        db: Any,
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        security_id: str,
        as_of: str,
    ) -> tuple[bool, str | None]:
        context = self._eligibility_context(
            decision,
            db,
            policy,
            snapshot,
            security_id,
            State.TRIM,
            True,
            decision.get("score"),
            decision.get("signal"),
            as_of,
        )
        eligibility = evaluate_eligibility(policy, State.TRIM, context)
        if eligibility.status != "PASS" or eligibility.to_state != State.EXIT:
            return False, None
        edge = (State.TRIM, State.EXIT)
        if eligibility.trigger_kind == "VERIFIED_THESIS_BREAK":
            verification = context.get("verified_break")
            if verification is not None and policy.allows_verified_break_bypass(*edge):
                decision["thesis_verification_id"] = verification["verification_id"]
                return True, "VERIFIED_THESIS_BREAK"
            return False, None
        # Same centralization as the normal path: every non-verified trigger
        # requires cooldown AND evidence persistence.
        cooldown_ok = cooldown_satisfied(
            decision.get("state_effective_at"), as_of, policy.cooldown_days(*edge)
        )
        decision["cooldown_satisfied"] = int(cooldown_ok)
        if not cooldown_ok:
            return False, None
        persistence = persistence_count(
            db,
            security_id,
            policy.policy_version,
            as_of,
            State.TRIM,
            State.EXIT,
            decision["decision_id"],
            decision["scored_evidence_hash"],
            eligibility.status,
            State.EXIT,
            hypothetical_evidence_driven=bool(decision.get("evidence_driven")),
        )
        decision["persistence_count"] = persistence
        required = policy.persistence_required(*edge)
        decision["persistence_required"] = required
        if persistence >= required:
            return True, "RULE_PERSISTED"
        material = self._material_change_satisfied(policy, edge, decision.get("score"), None)
        decision["material_change_satisfied"] = int(material)
        if material and policy.allows_material_bypass(*edge):
            return True, "MATERIAL_CHANGE"
        return False, None

    def _transition_row(
        self,
        decision: dict[str, Any],
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        security_id: str,
        from_state: State,
        to_state: State,
        cause: str,
        as_of: str,
        created_at: str,
    ) -> dict[str, Any]:
        assert cause in TRANSITION_CAUSES, cause
        decision_id = decision["decision_id"]
        transition_id = D(
            TRANSITION_TAG,
            C(
                {
                    "decision_id": decision_id,
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                    "cause": cause,
                }
            ),
        )
        score_snapshot_id = decision.get("score_snapshot_id") or decision.get(
            "settlement_score_snapshot_id"
        )
        if score_snapshot_id is None:
            raise ValueError(
                f"cannot record transition for {security_id} without a score snapshot lineage"
            )
        verification_id = decision.get("thesis_verification_id")
        if cause == "VERIFIED_THESIS_BREAK" and verification_id is None:
            raise ValueError("VERIFIED_THESIS_BREAK transition requires a verification id")
        return {
            "transition_id": transition_id,
            "decision_id": decision_id,
            "security_id": security_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
            "cause": cause,
            "reason_codes_json": json_roundtrip(sorted(decision["reason_codes"])),
            "score_snapshot_id": score_snapshot_id,
            "portfolio_snapshot_id": snapshot.snapshot_id,
            "policy_version": policy.policy_version,
            "thesis_break_verification_id": verification_id,
            "persistence_count": int(decision.get("persistence_count", 0)),
            "persistence_required": int(decision.get("persistence_required", 0)),
            "effective_at": as_of,
            "created_at": created_at,
        }

    def _write_transition(self, db: Any, transition: dict[str, Any]) -> None:
        # Chain continuity: effective_at must strictly advance and the latest
        # ledger state must equal this transition's from_state — a backdated or
        # forking write would silently corrupt the derived current state.
        prior = db.execute(
            "SELECT effective_at,to_state FROM portfolio_state_transition "
            "WHERE security_id=? ORDER BY effective_at DESC,created_at DESC LIMIT 1",
            (transition["security_id"],),
        ).fetchone()
        if prior is not None:
            if transition["effective_at"] <= prior["effective_at"]:
                raise ValueError(
                    f"backdated transition for {transition['security_id']}: "
                    f"new effective_at {transition['effective_at']} is not after "
                    f"latest {prior['effective_at']}"
                )
            if prior["to_state"] != transition["from_state"]:
                raise ValueError(
                    f"transition chain discontinuity for {transition['security_id']}: "
                    f"ledger head is {prior['to_state']} but new transition starts from "
                    f"{transition['from_state']}"
                )
        db.execute(
            "INSERT INTO portfolio_state_transition("
            "transition_id,decision_id,security_id,from_state,to_state,cause,reason_codes_json,"
            "score_snapshot_id,portfolio_snapshot_id,policy_version,thesis_break_verification_id,"
            "persistence_count,persistence_required,effective_at,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                transition[key]
                for key in (
                    "transition_id",
                    "decision_id",
                    "security_id",
                    "from_state",
                    "to_state",
                    "cause",
                    "reason_codes_json",
                    "score_snapshot_id",
                    "portfolio_snapshot_id",
                    "policy_version",
                    "thesis_break_verification_id",
                    "persistence_count",
                    "persistence_required",
                    "effective_at",
                    "created_at",
                )
            ),
        )

    def _draft(
        self,
        decision: dict[str, Any],
        transition: dict[str, Any],
        policy: PolicySpec,
        snapshot: PortfolioSnapshot,
        security_id: str,
        proposed_state: State,
        sizing_result: Any,
        reason_code: str,
        activity_date: str,
        created_at: str,
    ) -> dict[str, Any]:
        score = decision["score"]
        # a scoreless decision (verified thesis break without a current score)
        # carries neutral score-derived fields: conviction is not what
        # authorized the action, the verified break is
        conviction_ppm = round(score["conviction"] * 10_000) if score else 0
        data_quality_ppm = round(score["data_quality"] * 1_000_000) if score else 0
        agreement_ppm = (
            round(score["committee_agreement"] * 1_000_000)
            if score and score.get("committee_agreement") is not None
            else 0
        )
        trajectory = score["trajectory_label"] if score else "STABLE"  # neutral label
        proposal = build_proposal(
            decision_id=decision["decision_id"],
            transition_id=transition["transition_id"],
            activity_date=activity_date,
            security_id=security_id,
            current_state=State(transition["from_state"]),
            proposed_state=proposed_state,
            action=sizing_result.action,
            reason_codes=decision["reason_codes"] or [reason_code],
            conviction_ppm=conviction_ppm,
            data_quality_ppm=data_quality_ppm,
            agreement_ppm=agreement_ppm,
            trajectory=trajectory,
            current_weight_ppm=sizing_result.current_weight_ppm,
            target_weight_ppm=sizing_result.target_weight_ppm,
            max_quantity_microunits=sizing_result.max_quantity_microunits,
            completion_quantity_microunits=sizing_result.completion_quantity_microunits,
            max_notional_microusd=sizing_result.max_notional_microusd,
            score_snapshot_id=decision["score_snapshot_id"] or transition["score_snapshot_id"],
            portfolio_snapshot_id=snapshot.snapshot_id,
            policy_version=policy.policy_version,
            sizing_policy_version=policy.sizing_policy_version,
            quantity_increment_microunits=int(
                policy.order_constraints["quantity_increment_microunits"]
            ),
            limit_only=bool(policy.order_constraints["limit_only"]),
            created_at=created_at,
            current_quantity_microunits=decision["holding"].get("quantity_microunits")
            if decision["holding"]
            else None,
            sellable_quantity_microunits=decision["holding"].get("sellable_quantity_microunits")
            if decision["holding"]
            else None,
            mark_price_microusd=(
                snapshot.market_input(security_id).get("mark_price_microusd")
                if snapshot.market_input(security_id)
                else None
            ),
        )
        category = (
            "verified_break"
            if reason_code == "thesis_broken"
            else (
                "risk_reduction"
                if reason_code == "risk_reduction"
                else (
                    "data_integrity"
                    if reason_code == "data_integrity"
                    else (
                        "policy_ineligible" if reason_code == "policy_ineligible" else "score_band"
                    )
                )
            )
        )
        return {**proposal, "category": category}

    def _write_proposal(self, db: Any, draft: dict[str, Any]) -> None:
        columns = (
            "proposal_id,decision_id,transition_id,activity_date,security_id,current_state,"
            "proposed_state,action,reason_codes_json,conviction_ppm,data_quality_ppm,agreement_ppm,"
            "trajectory,current_weight_ppm,target_weight_ppm,max_quantity_microunits,"
            "completion_quantity_microunits,max_notional_microusd,order_constraints_json,"
            "score_snapshot_id,portfolio_snapshot_id,policy_version,sizing_policy_version,"
            "proposal_mode,requires_human_approval,created_at"
        )
        db.execute(
            f"INSERT INTO trade_proposal({columns}) VALUES ({','.join('?' for _ in range(26))})",
            tuple(
                draft[key]
                for key in (
                    "proposal_id",
                    "decision_id",
                    "transition_id",
                    "activity_date",
                    "security_id",
                    "current_state",
                    "proposed_state",
                    "action",
                    "reason_codes_json",
                    "conviction_ppm",
                    "data_quality_ppm",
                    "agreement_ppm",
                    "trajectory",
                    "current_weight_ppm",
                    "target_weight_ppm",
                    "max_quantity_microunits",
                    "completion_quantity_microunits",
                    "max_notional_microusd",
                    "order_constraints_json",
                    "score_snapshot_id",
                    "portfolio_snapshot_id",
                    "policy_version",
                    "sizing_policy_version",
                    "proposal_mode",
                    "requires_human_approval",
                    "created_at",
                )
            ),
        )

    def _observation_row(self, decision: dict[str, Any]) -> tuple:
        decision_input_hash = C(
            {
                "run_id": decision["run_id"],
                "security_id": decision["security_id"],
                "prior_state": decision["current_state"].value,
                "score_snapshot_id": decision.get("score_snapshot_id"),
                "signal_input_id": decision.get("signal_input_id"),
                "policy_version": decision.get("policy_version"),
                "signal_status": decision["signal_status"],
                "final_status": decision["final_status"].value,
                "reason_codes": sorted(decision["reason_codes"]),
                "risk": decision["risk_json"],
                "sizing": decision["sizing_json"],
            }
        )
        return (
            decision["decision_id"],
            decision["run_id"],
            decision["security_id"],
            decision["current_state"].value,
            decision["signal_state"],
            decision["proposed_state"],
            decision.get("score_snapshot_id"),
            decision.get("signal_input_id"),
            decision["portfolio_snapshot_id"],
            decision["policy_version"],
            decision.get("scored_evidence_hash"),
            decision.get("change_cause"),
            int(decision.get("evidence_driven", 0)),
            decision["signal_status"],
            int(decision.get("persistence_count", 0)),
            int(decision.get("persistence_required", 0)),
            int(decision.get("material_change_satisfied", 0)),
            int(decision.get("cooldown_satisfied", 1)),
            decision.get("risk_status", "NOT_RUN"),
            decision["final_status"].value,
            json_roundtrip(sorted(decision["reason_codes"])),
            json_roundtrip(decision["risk_json"]),
            json_roundtrip(decision["sizing_json"]),
            decision_input_hash,
            decision["as_of"],
            decision["created_at"],
        )

    def _stored_proposals(self, db: Any, run_id: str) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT p.* FROM trade_proposal p "
            "JOIN portfolio_state_observation o ON o.decision_id=p.decision_id "
            "WHERE o.run_id=? ORDER BY p.security_id",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _data_status(self, snapshot: PortfolioSnapshot, policy: PolicySpec) -> list[str]:
        status: list[str] = []
        if snapshot.cash_status.value != "KNOWN":
            status.append(f"cash: {snapshot.cash_status.value}")
        if snapshot.valuation_status.value != "KNOWN":
            status.append(f"valuation: {snapshot.valuation_status.value}")
        if snapshot.holdings_status.value != "KNOWN":
            status.append(f"holdings: {snapshot.holdings_status.value}")
        for market in snapshot.market_inputs:
            if market.get("price_status", "KNOWN") != "KNOWN":
                status.append(f"{market['security_id']} price: {market.get('price_status')}")
            if market.get("liquidity_status", "KNOWN") != "KNOWN":
                status.append(
                    f"{market['security_id']} liquidity: {market.get('liquidity_status')}"
                )
        return status

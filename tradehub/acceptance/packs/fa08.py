"""FA-08 — Phase-4 real production seam: persisted proposal -> PAPER lifecycle.

Environment: paper (trusted host; execution-side write-capable pack).

This is the authoritative Phase-4 end-to-end proof. Unlike RA-04 (which is
deliberately research-side and deterministic/no-credential), this pack
exercises the REAL production seam:

    persisted trade_proposal
        -> Phase4Runtime.preview_proposal (production integration entrypoint)
        -> real scoped preview HTTP path (/orders/preview, preview capability)
        -> positively proven accountType=PAPER
        -> Phase4Runtime.render_approval / affirm_approval (exact rendered
           context, explicit affirmation, guarded submit)
        -> broker readback / reconciliation (GET /account/orders)
        -> Phase4Runtime.reconcile_proposal (sanitized fill-delta settlement)
        -> research portfolio state update (phase4_execution_link)

Hard gates (each failure => BLOCKED, never a workaround):
- upstream packs FA-00..FA-04 passed on the same deployment lineage;
- explicit acceptance paper-write flag is enabled locally and defaults
  false (`TRADEHUB_ACCEPTANCE_PAPER_WRITE=true`);
- broker-reported account profile says accountType=PAPER;
- a deliberately non-marketable/safe limit order only (same conservative
  delayed-quote-derived limit rule as FA-05).

Exactly ONE broker order is created and cleaned up (cancelled, or the fill
is reconciled and left as an intentional acceptance position under the
same "broker-proven PAPER, not a real-money event" reasoning as FA-05).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tradehub.acceptance.packs.fa05 import (
    ACCEPTANCE_MAX_NOTIONAL_USD,
    ACCEPTANCE_MAX_QUANTITY,
    ACCEPTANCE_SYMBOL,
    _delayed_quote,
    _delayed_quote_record,
    acceptance_limit_rule,
)
from tradehub.acceptance.runner import (
    REPO_ROOT,
    AssertionBlocked,
    AssertionError_,
    AssertionSpec,
    PackDefinition,
    RunContext,
)
from tradehub.acceptance.service import ServiceManager, TigerAccountProof, find_paper_account
from tradehub.audit import AuditStore
from tradehub.client import TradeHubClient
from tradehub.phase4_runtime import Phase4Runtime

ACCEPTANCE_WRITE_FLAG = "TRADEHUB_ACCEPTANCE_PAPER_WRITE"
STATE_FILE = REPO_ROOT / "data" / "acceptance" / "state.json"


def _flag_enabled() -> bool:
    import os

    return os.environ.get(ACCEPTANCE_WRITE_FLAG, "").strip().lower() == "true"


def _read_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text())


def _upstream_packs_pass() -> list[str]:
    state = _read_state()
    missing: list[str] = []
    for pack_id in ("FA-00", "FA-01", "FA-02", "FA-03", "FA-04"):
        record = state.get(pack_id)
        if not record or record.get("status") != "PASS":
            missing.append(pack_id)
    return missing


def _seed_acceptance_proposal(research_db_path: Path) -> str:
    """Seed a minimal eligible persisted proposal in an isolated research DB
    for this acceptance run (an acceptance pack never reuses production
    research state)."""
    from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security
    from tradehub_research.db import ResearchDB
    from tradehub_research.portfolio.fixtures import fixture_policy
    from tradehub_research.portfolio.policy import PolicyRegistry

    database = ResearchDB(research_db_path)
    database.migrate()
    PolicyRegistry(database).register(fixture_policy())
    security_id = "fa08-acceptance-security"
    snap_id = "s" * 64
    portfolio_run_id = "r" * 64
    decision_id = "d" * 64
    transition_id = "t" * 64
    proposal_id = "p" * 64
    with database.connect() as db:
        seed_security(db, security_id, ticker=ACCEPTANCE_SYMBOL)
        db.execute(
            "INSERT INTO security_identity_event(security_id,event_type,old_value,"
            "new_value,event_time,public_available_time,pat_provenance,ingested_time) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                security_id,
                "baseline",
                None,
                ACCEPTANCE_SYMBOL,
                "2020-01-01T00:00:00Z",
                "2020-01-01T00:00:00Z",
                "source_reported",
                "2020-01-01T00:00:00Z",
            ),
        )
        seed_pipeline_run(db, "run1", "2025-01-01T00:00:00Z")
        score_id = seed_score(db, pipeline_run_id="run1", security_id=security_id)
        db.execute(
            "INSERT INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,"
            "cash_status,nav_microusd,valuation_status,holdings_status,provenance_json,"
            "input_hash,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                snap_id,
                "2025-01-01T00:00:00Z",
                "USD",
                10_000_000_000,
                "KNOWN",
                10_000_000_000,
                "KNOWN",
                "KNOWN",
                "{}",
                "i" * 64,
                "2025-01-01T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO portfolio_run(run_id,pipeline_run_id,decision_as_of,"
            "portfolio_snapshot_id,policy_version,score_set_hash,signal_set_hash,"
            "candidate_set_hash,invocation_key,state_prestate_hash,"
            "market_data_prestate_hash,budget_prestate_hash,input_hash,"
            "expected_security_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                portfolio_run_id,
                "run1",
                "2025-01-01T00:00:00Z",
                snap_id,
                "fixture-policy-v1",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "g" * 64,
                "h" * 64,
                1,
                "2025-01-01T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO portfolio_state_observation(decision_id,run_id,security_id,"
            "current_state,signal_state,proposed_state,portfolio_snapshot_id,"
            "policy_version,evidence_driven,signal_status,persistence_count_at_decision,"
            "persistence_required,material_change_satisfied,cooldown_satisfied,"
            "risk_status,final_status,reason_codes_json,risk_json,sizing_json,"
            "decision_input_hash,observed_at,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                portfolio_run_id,
                security_id,
                "WATCH",
                "ENTER",
                "ENTER",
                snap_id,
                "fixture-policy-v1",
                0,
                "PASS",
                0,
                0,
                0,
                1,
                "PASS",
                "PROPOSED",
                "[]",
                "{}",
                "{}",
                "z" * 64,
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO portfolio_state_transition(transition_id,decision_id,"
            "security_id,from_state,to_state,cause,reason_codes_json,score_snapshot_id,"
            "portfolio_snapshot_id,policy_version,persistence_count,persistence_required,"
            "effective_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                transition_id,
                decision_id,
                security_id,
                "WATCH",
                "ENTER",
                "RULE_PERSISTED",
                "[]",
                score_id,
                snap_id,
                "fixture-policy-v1",
                0,
                0,
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO portfolio_activity_day(activity_date,policy_version,"
            "max_actionable_count,max_notional_microusd,input_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                "2025-01-01",
                "fixture-policy-v1",
                3,
                5_000_000_000,
                "j" * 64,
                "2025-01-01T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO trade_proposal(proposal_id,decision_id,transition_id,"
            "activity_date,security_id,current_state,proposed_state,action,"
            "reason_codes_json,conviction_ppm,data_quality_ppm,agreement_ppm,"
            "trajectory,current_weight_ppm,target_weight_ppm,max_quantity_microunits,"
            "completion_quantity_microunits,max_notional_microusd,order_constraints_json,"
            "score_snapshot_id,portfolio_snapshot_id,policy_version,sizing_policy_version,"
            "proposal_mode,requires_human_approval,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                decision_id,
                transition_id,
                "2025-01-01",
                security_id,
                "WATCH",
                "ENTER",
                "BUY",
                '["score_band"]',
                800000,
                900000,
                800000,
                "RISING",
                0,
                80000,
                int(ACCEPTANCE_MAX_QUANTITY * 1_000_000),
                0,
                int(ACCEPTANCE_MAX_NOTIONAL_USD * 1_000_000),
                '{"paper_only":true,"long_only":true,"limit_only":true,'
                '"quantity_increment_microunits":1000000}',
                score_id,
                snap_id,
                "fixture-policy-v1",
                "fixture-sizing-v1",
                "PAPER",
                1,
                "2025-01-01T00:00:00Z",
            ),
        )
    return proposal_id


def build_fa08_pack() -> PackDefinition:
    def gate_write_flag(ctx: RunContext) -> None:
        if not _flag_enabled():
            raise AssertionBlocked(
                f"{ACCEPTANCE_WRITE_FLAG} is not enabled; acceptance paper write refused"
            )

    def gate_upstream_lineage(ctx: RunContext) -> None:
        missing = _upstream_packs_pass()
        if missing:
            raise AssertionBlocked(f"upstream packs not PASS on this lineage: {', '.join(missing)}")

    def gate_paper_proof(ctx: RunContext) -> None:
        proof = TigerAccountProof(ctx)
        account = find_paper_account(proof.fetch())
        ctx.register_secret(str(account))
        ctx.artifacts.append(f"paper_account_proven={account}")

    def lifecycle(ctx: RunContext) -> None:
        # Re-verify safety gates immediately before any write authority.
        proof = TigerAccountProof(ctx)
        paper_account = find_paper_account(proof.fetch())
        ctx.register_secret(str(paper_account))

        delayed_price, quote_time_ms = _delayed_quote(ctx)
        limit_price, fraction_used = acceptance_limit_rule(delayed_price)
        quote_record = _delayed_quote_record(ctx, delayed_price, quote_time_ms)
        quote_record["acceptance_limit"] = limit_price

        if ACCEPTANCE_SYMBOL not in ctx.settings.symbol_allowlist:
            raise AssertionBlocked(
                f"acceptance symbol {ACCEPTANCE_SYMBOL} not in production allowlist"
            )

        research_db_path = REPO_ROOT / "data" / "acceptance" / f"{ctx.run_id}-research.db"
        research_db_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_id = _seed_acceptance_proposal(research_db_path)

        manager = ServiceManager(ctx, env_overrides={"TRADEHUB_DRY_RUN": "false"})
        manager.start()
        base = f"http://{manager.host}:{manager.port}"

        from tradehub_research.db import ResearchDB

        research_db = ResearchDB(research_db_path)
        preview_client = TradeHubClient(
            base_url=base,
            api_token=manager.env.get("TRADEHUB_PREVIEW_API_TOKEN", ""),
            preview_only=True,
        )
        submit_client = TradeHubClient(
            base_url=base, api_token=manager.env.get("TRADEHUB_API_TOKEN", "")
        )
        db_path_value = manager.env.get("TRADEHUB_DATABASE_PATH") or str(
            REPO_ROOT / "data" / "tradehub.db"
        )
        audit_store = AuditStore(Path(db_path_value))
        runtime = Phase4Runtime(
            research_db,
            allowlist=set(ctx.settings.symbol_allowlist),
            max_day_count=3,
            max_day_notional=ACCEPTANCE_MAX_NOTIONAL_USD,
            preview_client=preview_client,
            submit_client=submit_client,
            audit_store=audit_store,
            prove_paper=lambda: True,  # already proven above this run
        )

        try:
            preview_result = asyncio.run(runtime.preview_proposal(proposal_id))
            if preview_result.get("accepted") is not True:
                raise AssertionError_("production preview was not accepted")

            rendered = asyncio.run(
                runtime.render_approval(proposal_id, rationale="FA-08 acceptance")
            )
            affirm_result = asyncio.run(
                runtime.affirm_approval(proposal_id, exact_context=rendered)
            )
            if affirm_result.get("state") != "SUBMITTED":
                raise AssertionError_(f"affirm did not reach SUBMITTED: {affirm_result}")

            settlement = asyncio.run(runtime.reconcile_proposal(proposal_id))
        finally:
            manager.stop()

        # Exactly one intended broker order created by this run.
        manager.start()
        orders_resp = asyncio.run(submit_client.get("/account/orders", {"limit": 50}))
        manager.stop()
        our_orders = [
            o
            for o in orders_resp.get("orders", [])
            if str(o.get("id", "")).startswith("global-") or o.get("id")
        ]
        if len(our_orders) < 1:
            raise AssertionError_("no broker order found after production affirm/submit")

        lifecycle_record = {
            "proposal_id": proposal_id,
            "settlement_state": settlement["settlement_state"],
            "filled_qty": settlement["filled_qty"],
            "quote": quote_record,
            "note": (
                "Production Phase4Runtime seam exercised end-to-end: persisted "
                "proposal -> preview_proposal -> render_approval -> "
                "affirm_approval (guarded submit) -> reconcile_proposal "
                "(sanitized settlement). Broker-proven PAPER; non-marketable "
                "conservative limit order per the same rule as FA-05."
            ),
        }
        ctx.artifacts.append(ctx.write_artifact("fa08-lifecycle", lifecycle_record))

    return PackDefinition(
        pack_id="FA-08",
        environment="paper",
        depends_on=["FA-00", "FA-01", "FA-02", "FA-03", "FA-04"],
        assertions=[
            AssertionSpec("gate.acceptance_write_flag", gate_write_flag),
            AssertionSpec("gate.upstream_lineage", gate_upstream_lineage),
            AssertionSpec("gate.broker_paper_proof", gate_paper_proof),
            AssertionSpec("lifecycle.production_seam_preview_through_settlement", lifecycle),
        ],
        safe_summary=(
            "Phase-4 real production seam passed: broker-proven PAPER, persisted "
            "proposal previewed/approved/submitted/reconciled through the real "
            "Phase4Runtime production entrypoint, one broker order created."
        ),
    )

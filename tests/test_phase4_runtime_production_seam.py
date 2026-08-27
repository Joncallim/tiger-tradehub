"""Production-seam wiring test: exercises Phase4Runtime through a REAL
FastAPI/uvicorn HTTP server (not TestClient in-process calls) and a real
sqlite-backed ResearchDB + AuditStore, proving the full
preview -> render -> affirm -> submit -> reconcile -> settle sequence end to
end via the same TradeHubClient the production MCP server uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time

import pytest
import uvicorn

from tests.portfolio_test_helpers import seed_pipeline_run, seed_score, seed_security
from tradehub.app import app, get_gateway, get_settings, get_store
from tradehub.audit import AuditStore
from tradehub.client import TradeHubClient
from tradehub.config import Settings
from tradehub.phase4_runtime import Phase4Runtime
from tradehub_research.db import ResearchDB

STRONG_TOKEN = "test-token-with-enough-length"
PREVIEW_TOKEN = "preview-token-with-enough-length"


class FakeOrder:
    def __init__(self, order_id: str):
        self.order_id = order_id


class FakeGateway:
    """Deterministic broker double: preview always accepted, orders fill
    across a scripted sequence of statuses when reconciled."""

    def __init__(self, fill_sequence: list[dict[str, object]] | None = None):
        self.is_configured_result = True
        self.placed_orders: list[str] = []
        self.fill_sequence = fill_sequence or [{"status": "SUBMITTED", "filled": 0}]
        self._fill_index = 0
        self.broker_orders: dict[str, dict[str, object]] = {}

    def is_configured(self):
        return self.is_configured_result

    def preview_order(self, intent):
        return {
            "init_margin_before": 0,
            "init_margin": 0,
            "maint_margin_before": 0,
            "maint_margin": 0,
            "margin_currency": "USD",
            "equity_with_loan_before": 100000,
            "equity_with_loan": 100000,
            "min_commission": 0,
            "max_commission": 0,
            "commission_currency": "USD",
        }

    def create_order(self, intent):
        order_id = f"reserved-{len(self.placed_orders) + 1}"
        return FakeOrder(order_id)

    def place_order(self, order):
        order_id = order.order_id
        self.placed_orders.append(order_id)
        response = {"id": f"global-{order_id}", "order_id": str(order_id)}
        self.broker_orders[str(order_id)] = response
        return f"global-{order_id}", response

    def assign_order_id(self, order, order_id):
        order.order_id = order_id
        return order

    def get_order_id(self, order):
        return str(order.order_id)

    def get_order(self, order_id):
        current = self.fill_sequence[min(self._fill_index, len(self.fill_sequence) - 1)]
        return {"id": f"global-{order_id}", "order_id": str(order_id), **current}

    def get_orders(self, symbol=None, limit=20):
        current = self.fill_sequence[min(self._fill_index, len(self.fill_sequence) - 1)]
        return [
            {"id": f"global-{order_id}", "order_id": str(order_id), **current}
            for order_id in self.placed_orders
        ]

    def advance_fill(self) -> None:
        self._fill_index += 1

    def cancel_order(self, order_id):
        return {"cancelled": True}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def running_server(settings: Settings, store: AuditStore, gateway: FakeGateway):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_gateway] = lambda: gateway
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _run():
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()


def _seed_proposal(db_path, *, security_id="sec1", action="BUY", proposed_state="ENTER"):

    database = ResearchDB(db_path)
    database.migrate()
    from tradehub_research.portfolio.fixtures import fixture_policy
    from tradehub_research.portfolio.policy import PolicyRegistry

    PolicyRegistry(database).register(fixture_policy())
    snap_id = "s" * 64
    portfolio_run_id = "r" * 64
    decision_id = "d" * 64
    transition_id = "t" * 64
    with database.connect() as db:
        seed_security(db, security_id, ticker="AAPL")
        db.execute(
            "INSERT INTO security_identity_event(security_id,event_type,old_value,"
            "new_value,event_time,public_available_time,pat_provenance,ingested_time) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                security_id,
                "baseline",
                None,
                "AAPL",
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
                "WATCH" if action == "BUY" else "HOLD",
                proposed_state,
                proposed_state,
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
                "WATCH" if action == "BUY" else "HOLD",
                proposed_state,
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
            ("2025-01-01", "fixture-policy-v1", 3, 5_000_000_000, "j" * 64, "2025-01-01T00:00:00Z"),
        )
        reason = '["score_band"]' if action == "BUY" else '["thesis_broken"]'
        current_weight, target_weight = (0, 80000) if action == "BUY" else (80000, 0)
        completion_qty = 0
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
                "p" * 64,
                decision_id,
                transition_id,
                "2025-01-01",
                security_id,
                "WATCH" if action == "BUY" else "HOLD",
                proposed_state,
                action,
                reason,
                800000,
                900000,
                800000,
                "RISING" if action == "BUY" else "FALLING",
                current_weight,
                target_weight,
                1_000_000,
                completion_qty,
                150_000_000,
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
    return database, "p" * 64


@pytest.fixture
def audit_store(tmp_path):
    return AuditStore(tmp_path / "tradehub.db")


@pytest.fixture
def settings(tmp_path):
    return Settings(
        TRADEHUB_API_TOKEN=STRONG_TOKEN,
        TRADEHUB_PREVIEW_API_TOKEN=PREVIEW_TOKEN,
        TRADEHUB_DATABASE_PATH=tmp_path / "tradehub.db",
        TRADEHUB_DRY_RUN=False,
    )


def test_full_production_seam_preview_through_settlement(tmp_path, settings, audit_store):
    """persisted proposal -> preview_persisted_proposal -> render approval ->
    affirm -> submit -> reconcile -> fill-delta settlement, all through the
    real HTTP server and real AuditStore -- the authoritative Phase-4
    end-to-end proof for the code path (broker double stands in for Tiger)."""
    database, proposal_id = _seed_proposal(tmp_path / "research.db")
    gateway = FakeGateway(
        fill_sequence=[
            {"status": "SUBMITTED", "filled": 0.4},
            {"status": "FILLED", "filled": 1.0},
        ]
    )

    with running_server(settings, audit_store, gateway) as base_url:
        preview_client = TradeHubClient(
            base_url=base_url, api_token=PREVIEW_TOKEN, preview_only=True
        )
        submit_client = TradeHubClient(base_url=base_url, api_token=STRONG_TOKEN)
        runtime = Phase4Runtime(
            database,
            allowlist={"AAPL"},
            max_day_count=3,
            max_day_notional=1000,
            preview_client=preview_client,
            submit_client=submit_client,
            audit_store=audit_store,
            prove_paper=lambda: True,
        )

        preview_result = asyncio.run(runtime.preview_proposal(proposal_id))
        assert preview_result["accepted"] is True

        rendered = asyncio.run(runtime.render_approval(proposal_id))
        assert rendered["symbol"] == "AAPL"
        assert rendered["side"] == "BUY"

        affirm_result = asyncio.run(runtime.affirm_approval(proposal_id, exact_context=rendered))
        assert affirm_result["state"] == "SUBMITTED"
        assert len(gateway.placed_orders) == 1  # exactly one broker order created

        first_settlement = asyncio.run(runtime.reconcile_proposal(proposal_id))
        assert first_settlement["settlement_state"] == "PARTIALLY_FILLED"
        assert first_settlement["owned_quantity"] == pytest.approx(0.4)
        assert first_settlement["next_state"] == "ENTER"  # nonterminal: still pending

        gateway.advance_fill()
        second_settlement = asyncio.run(runtime.reconcile_proposal(proposal_id))
        assert second_settlement["owned_quantity"] == pytest.approx(1.0)  # delta only
        assert second_settlement["next_state"] == "HOLD"  # terminal: transition completes

        # Repeated identical reconciliation applies zero further delta.
        third_settlement = asyncio.run(runtime.reconcile_proposal(proposal_id))
        assert third_settlement["owned_quantity"] == pytest.approx(1.0)
        assert not third_settlement["portfolio_mutated"]

        # Exactly one broker order was ever created across the whole flow.
        assert len(gateway.placed_orders) == 1


def test_affirm_rejects_altered_context_through_the_production_seam(
    tmp_path, settings, audit_store
):
    database, proposal_id = _seed_proposal(tmp_path / "research.db")
    gateway = FakeGateway()

    with running_server(settings, audit_store, gateway) as base_url:
        preview_client = TradeHubClient(
            base_url=base_url, api_token=PREVIEW_TOKEN, preview_only=True
        )
        submit_client = TradeHubClient(base_url=base_url, api_token=STRONG_TOKEN)
        runtime = Phase4Runtime(
            database,
            allowlist={"AAPL"},
            max_day_count=3,
            max_day_notional=1000,
            preview_client=preview_client,
            submit_client=submit_client,
            audit_store=audit_store,
            prove_paper=lambda: True,
        )

        asyncio.run(runtime.preview_proposal(proposal_id))
        rendered = asyncio.run(runtime.render_approval(proposal_id))
        altered = {**rendered, "quantity": 999}

        with pytest.raises((ValueError, KeyError, TypeError)):
            asyncio.run(runtime.affirm_approval(proposal_id, exact_context=altered))
        assert gateway.placed_orders == []


def test_restart_recovery_resumes_after_process_restart(tmp_path, settings, audit_store):
    """A fresh Phase4Runtime instance (simulating process restart) recovers
    authority from AuditStore + the safe research-side link and completes
    submit + reconciliation."""
    database, proposal_id = _seed_proposal(tmp_path / "research.db")
    gateway = FakeGateway(fill_sequence=[{"status": "FILLED", "filled": 1.0}])

    with running_server(settings, audit_store, gateway) as base_url:
        preview_client = TradeHubClient(
            base_url=base_url, api_token=PREVIEW_TOKEN, preview_only=True
        )
        submit_client = TradeHubClient(base_url=base_url, api_token=STRONG_TOKEN)

        runtime_a = Phase4Runtime(
            database,
            allowlist={"AAPL"},
            max_day_count=3,
            max_day_notional=1000,
            preview_client=preview_client,
            submit_client=submit_client,
            audit_store=audit_store,
            prove_paper=lambda: True,
        )
        asyncio.run(runtime_a.preview_proposal(proposal_id))

        # Simulate restart: a brand-new runtime instance, no in-memory state
        # carried over, recovers purely from the DB + AuditStore.
        runtime_b = Phase4Runtime(
            database,
            allowlist={"AAPL"},
            max_day_count=3,
            max_day_notional=1000,
            preview_client=preview_client,
            submit_client=submit_client,
            audit_store=audit_store,
            prove_paper=lambda: True,
        )
        rendered = asyncio.run(runtime_b.render_approval(proposal_id))
        affirm_result = asyncio.run(runtime_b.affirm_approval(proposal_id, exact_context=rendered))
        assert affirm_result["state"] == "SUBMITTED"
        settlement = asyncio.run(runtime_b.reconcile_proposal(proposal_id))
        assert settlement["settlement_state"] == "FILLED"
        assert len(gateway.placed_orders) == 1

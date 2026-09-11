"""Persisted Phase-3 export reaches the Phase-6 dry-run runner.

This crosses the actual file-envelope boundary.  It deliberately uses the
real portfolio engine and the registered non-FIXTURE PAPER provisional policy;
the only substitute is the execution API client, so no broker is contacted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tests.portfolio_test_helpers import (
    seed_pipeline_run,
    seed_price_bars,
    seed_score,
    seed_security,
)
from tradehub.autonomy import policy as autonomy_policy
from tradehub.autonomy.runner import run_autonomy
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.ops.decision_pipeline import (
    ensure_paper_provisional_policy,
    export_eligible_proposals,
)
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.snapshot import build_signal_input, build_snapshot


class _DryRunExecutionClient:
    """Execution-API substitute: proves the existing guarded dry-run route."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str) -> dict:
        self.calls.append((path, None))
        if path == "/account/proof":
            return {
                "environment": "LIVE",
                "account": "paper-test-account",
                "account_type": "PAPER",
                "account_status": "Funded",
                "assets_ok": True,
            }
        if path == "/config/allowlist":
            return {"symbols": ["AAPL"]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if path == "/orders/preview":
            return {"accepted": True, "confirmation_token": "dry-run-token"}
        if path == "/orders/submit":
            # This is the execution boundary assertion: the runner receives a
            # dry-run receipt, so it cannot represent a broker write.
            return {"submitted": True, "dry_run": True, "order_id": "dry-run-order"}
        raise AssertionError(f"unexpected POST {path}")


def _snapshot(as_of: str):
    return build_snapshot(
        as_of,
        cash_microusd=10_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[],
        market_inputs=[
            {
                "security_id": "aapl-security",
                "mark_price_microusd": 50_000_000,
                "price_as_of": as_of,
                # The risk ledger reconciles its trailing 20 sessions: the
                # last 20 $50..$56 / 1m-share bars average $52.90m.
                "avg_dollar_volume_microusd": 52_900_000_000_000,
                "liquidity_as_of": as_of,
                "evidence_ids": [f"aapl-security:bar:{index:03d}" for index in range(40)],
            }
        ],
    )


def test_persisted_nonfixture_proposal_exports_once_and_receives_dry_run_receipt(tmp_path):
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    assert ensure_paper_provisional_policy(database) == "paper-provisional-v1"
    engine = PortfolioEngine(database)
    as_ofs = [
        "2025-06-01T00:00:00Z",
        "2025-06-03T00:00:00Z",
        "2025-06-05T00:00:00Z",
        "2025-06-07T00:00:00Z",
    ]
    with database.connect() as conn:
        seed_security(conn, "aapl-security", ticker="AAPL")
        seed_price_bars(
            conn,
            "aapl-security",
            closes=[50.0 + (index % 7) for index in range(40)],
            start_date="2025-04-22",
        )

    summaries = []
    for index, as_of in enumerate(as_ofs):
        run_id = f"pipeline-{index}"
        with database.connect() as conn:
            seed_pipeline_run(conn, run_id, as_of)
            seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="aapl-security",
                conviction=80,
                trajectory_label="RISING",
                change_cause="INITIAL" if index == 0 else "EVIDENCE_DRIVEN",
                material_change_time=as_of,
                prior_conviction=80 if index else None,
                conviction_delta=0 if index else None,
                scored_evidence_hash=f"evidence-{index}",
                run_as_of=as_of,
                committee_suffix=str(index),
            )
        summaries.append(
            engine.run(
                pipeline_run_id=run_id,
                policy_version="paper-provisional-v1",
                snapshot=_snapshot(as_of),
                decision_as_of=as_of,
                signals=[
                    build_signal_input("aapl-security", as_of, remaining_opportunity_ppm=500_000)
                ],
                allow_provisional=True,
                allow_fixture=False,
            )
        )

    proposal_run = next(summary for summary in summaries if summary.proposal_count == 1)
    inbox = tmp_path / "proposal-inbox"
    first_export = export_eligible_proposals(database, run_id=proposal_run.run_id, inbox=inbox)
    second_export = export_eligible_proposals(database, run_id=proposal_run.run_id, inbox=inbox)
    assert first_export["eligible_exports"] == second_export["eligible_exports"]
    proposal_id = first_export["eligible_exports"][0]
    envelope = json.loads((inbox / f"{proposal_id}.json").read_text())
    assert envelope["proposal"]["proposal_mode"] == "PAPER"
    assert envelope["proposal"]["policy_version"] == "paper-provisional-v1"

    policy_path = tmp_path / "paper-autonomy-policy.json"
    policy_path.write_text(json.dumps(autonomy_policy.default_policy_payload()))
    client = _DryRunExecutionClient()
    proposal_time = datetime.fromisoformat(
        str(envelope["proposal"]["created_at"]).replace("Z", "+00:00")
    )
    receipt = run_autonomy(
        settings=ResearchSettings(db_path=database.path, api_token="test-token"),
        policy_path=policy_path,
        inbox=inbox,
        ledger=tmp_path / "paper-run-ledger.jsonl",
        budget_db=tmp_path / "paper-budget.sqlite",
        api_client=client,
        now=proposal_time.replace(tzinfo=timezone.utc),
    )

    assert receipt["status"] == "OK"
    assert receipt["orders"] == 1, receipt["refusals"]
    assert receipt["executions"] == [
        {
            "proposal_id": proposal_id,
            "symbol": "AAPL",
            "action": "BUY",
            "decision": "EXECUTED",
            "dry_run": True,
            "submitted": True,
            "order_id": "dry-run-order",
            "reconcile_status": "DRY_RUN_NO_ORDER",
            "submit_error": None,
            "reconcile_error": None,
            "at": receipt["executions"][0]["at"],
        }
    ]
    assert [path for path, _ in client.calls] == [
        "/account/proof",
        "/config/allowlist",
        "/orders/preview",
        "/orders/submit",
    ]
    assert not list(inbox.glob("*.json"))
    assert (inbox / "processed" / f"{proposal_id}.json").is_file()

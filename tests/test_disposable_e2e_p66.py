"""Disposable-database E2E for PR #66's production runner boundary.

Uses a FRESH migrated temp database (the disposable DB) with the minimum copied
state — never the canonical production research.db. Drives the REAL chain:

    persisted proposal -> exporter -> authority record -> envelope
      -> production runner (fixture_mode=False)
      -> PAPER account proof -> preview -> DRY-RUN submit -> honest receipt

and then the refusal matrix around it. No broker is contacted: the execution
API is a deterministic stand-in that only records the calls it receives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.portfolio_test_helpers import (
    seed_pipeline_run,
    seed_price_bars,
    seed_score,
    seed_security,
)
from tradehub.autonomy.runner import run_autonomy
from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.ops.decision_pipeline import (
    ensure_paper_provisional_policy,
    export_eligible_proposals,
)
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.snapshot import build_signal_input, build_snapshot

SECURITY = "aapl-security"
TICKER = "AAPL"
NOW = "2025-06-09T00:00:00Z"


class DryRunExecutionClient:
    """Execution-API stand-in: proves the existing guarded dry-run route."""

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
            return {"symbols": [TICKER]}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        if path == "/orders/preview":
            return {"accepted": True, "confirmation_token": "dry-run-token"}
        if path == "/orders/submit":
            # The execution boundary assertion: a dry-run receipt, so this
            # cannot represent a broker write.
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
                "security_id": SECURITY,
                "mark_price_microusd": 50_000_000,
                "price_as_of": as_of,
                "avg_dollar_volume_microusd": 52_900_000_000_000,
                "liquidity_as_of": as_of,
                "evidence_ids": [f"{SECURITY}:bar:{i:03d}" for i in range(40)],
            }
        ],
    )


@pytest.fixture()
def disposable(tmp_path) -> dict:
    """A fresh, migrated, DISPOSABLE research database plus runner wiring."""
    db_path = tmp_path / "disposable.db"
    database = ResearchDB(db_path)
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
        seed_security(conn, SECURITY, ticker=TICKER)
        seed_price_bars(
            conn,
            SECURITY,
            closes=[50.0 + (index % 7) for index in range(40)],
            start_date="2025-04-22",
        )

    proposal_run = None
    for index, as_of in enumerate(as_ofs):
        run_id = f"disposable-{index}"
        with database.connect() as conn:
            seed_pipeline_run(conn, run_id, as_of)
            seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id=SECURITY,
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
        summary = engine.run(
            pipeline_run_id=run_id,
            policy_version="paper-provisional-v1",
            snapshot=_snapshot(as_of),
            decision_as_of=as_of,
            signals=[build_signal_input(SECURITY, as_of, remaining_opportunity_ppm=500_000)],
            allow_provisional=True,
            allow_fixture=False,
        )
        if summary.proposal_count:
            proposal_run = summary

    assert proposal_run is not None, "the engine produced no persisted proposal"

    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    policy_file = policy_dir / "paper_policy.json"
    from tradehub.autonomy import policy as autonomy_policy

    policy_file.write_text(json.dumps(autonomy_policy.default_policy_payload()))

    inbox = tmp_path / "inbox"
    authority_dir = tmp_path / "authority"

    return {
        "database": database,
        "db_path": db_path,
        "proposal_run": proposal_run,
        "inbox": inbox,
        "authority_dir": authority_dir,
        "policy_path": policy_file,
        "ledger": tmp_path / "ledger.jsonl",
        "budget_db": tmp_path / "budget.sqlite",
        "kill_file": tmp_path / "kill_switch",
        "settings": ResearchSettings(api_token="test-token"),
        "client": DryRunExecutionClient(),
    }


def _export(d: dict) -> dict:
    return export_eligible_proposals(
        d["database"],
        run_id=d["proposal_run"].run_id,
        inbox=d["inbox"],
        authority_dir=d["authority_dir"],
    )


def _envelopes(d: dict) -> list[Path]:
    return sorted(p for p in d["inbox"].glob("*.json") if p.is_file())


def _run(d: dict, **kwargs) -> dict:
    if "now" not in kwargs:
        # Deterministic clock: the disposable run happens just after the
        # exported proposal was created, so age checks are meaningful but not
        # tied to the wall clock.
        envelopes = _envelopes(d)
        if envelopes:
            envelope = json.loads(envelopes[0].read_text())
            created = envelope["proposal"].get("created_at") or envelope.get("exported_at")
            if created:
                from datetime import datetime, timedelta, timezone

                moment = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                kwargs["now"] = moment + timedelta(seconds=60)
                if kwargs["now"].tzinfo is None:
                    kwargs["now"] = kwargs["now"].replace(tzinfo=timezone.utc)
    kwargs.setdefault(
        "kill_switch_path",
        d.get("kill_file") or Path(d["ledger"]).parent / "kill_switch",
    )
    return run_autonomy(
        settings=d["settings"],
        policy_path=d["policy_path"],
        inbox=d["inbox"],
        ledger=d["ledger"],
        budget_db=d["budget_db"],
        api_client=d["client"],
        authority_dir=d["authority_dir"],
        fixture_mode=False,
        **kwargs,
    )


def _receipts(d: dict) -> list[dict]:
    if not d["ledger"].exists():
        return []
    return [json.loads(ln) for ln in d["ledger"].read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# the happy chain
# --------------------------------------------------------------------------
def test_disposable_e2e_full_chain_dry_run(disposable):  # noqa: F811
    exported = _export(disposable)
    envelopes = _envelopes(disposable)
    authority = sorted(p for p in disposable["authority_dir"].glob("*.json"))

    # authority published for the same proposal ids as the envelopes
    assert exported["proposal_count"] == len(envelopes) == 1
    assert len(authority) == 1
    assert authority[0].stem == envelopes[0].stem

    summary = _run(disposable)

    # PAPER proof was taken from the execution API, not assumed
    assert ("/account/proof", None) in disposable["client"].calls
    assert summary["orders"] == 1, summary["refusals"]

    previews = [p for path, p in disposable["client"].calls if path == "/orders/preview"]
    submits = [p for path, p in disposable["client"].calls if path == "/orders/submit"]
    assert len(previews) == 1 and len(submits) == 1

    # honest receipt: dry-run, not a broker write
    executions = summary["executions"]
    assert executions and executions[0]["decision"] == "EXECUTED"
    assert executions[0].get("dry_run") is True

    receipts = _receipts(disposable)
    assert receipts and receipts[-1]["kind"] == "runner_run_receipt_v1"
    assert receipts[-1]["orders"] == 1


def test_disposable_e2e_missing_authority_refuses(disposable):  # noqa: F811
    _export(disposable)
    for path in disposable["authority_dir"].glob("*.json"):
        path.unlink()
    summary = _run(disposable)
    assert summary["orders"] == 0
    assert any(
        "no persisted proposal authority" in str(r.get("reason", "")) for r in summary["refusals"]
    )
    assert [p for p, _ in disposable["client"].calls if p.startswith("/orders/")] == []


def test_disposable_e2e_tampered_envelope_refuses(disposable):  # noqa: F811
    _export(disposable)
    path = _envelopes(disposable)[0]
    envelope = json.loads(path.read_text())
    envelope["proposal"]["max_quantity_microunits"] *= 2  # tamper after export
    path.write_text(json.dumps(envelope))

    summary = _run(disposable)
    assert summary["orders"] == 0
    assert any(
        "does not match published authority" in str(r.get("reason", ""))
        for r in summary["refusals"]
    ), summary["refusals"]


def test_disposable_e2e_forged_fixture_refuses(disposable):  # noqa: F811
    _export(disposable)
    path = _envelopes(disposable)[0]
    envelope = json.loads(path.read_text())
    envelope["fixture"] = True
    envelope["fixture_tag"] = "paper-acceptance-fixture-v1"
    path.write_text(json.dumps(envelope))

    summary = _run(disposable)
    assert summary["orders"] == 0
    assert any("fixture authority" in str(r.get("reason", "")) for r in summary["refusals"]), (
        summary["refusals"]
    )
    assert [p for p, _ in disposable["client"].calls if p.startswith("/orders/")] == []


def test_disposable_e2e_tampered_authority_refuses(disposable):  # noqa: F811
    _export(disposable)
    record_path = sorted(disposable["authority_dir"].glob("*.json"))[0]
    record = json.loads(record_path.read_text())
    record["max_notional_microusd"] = int(record["max_notional_microusd"]) * 10  # inflate
    record_path.write_text(json.dumps(record))

    summary = _run(disposable)
    assert summary["orders"] == 0
    assert summary["refusals"]
    # The refusal must be ATTRIBUTABLE to the tamper, not any incidental cause.
    assert any(
        "does not match published authority" in str(entry.get("reason", ""))
        for entry in summary["refusals"]
    ), summary["refusals"]
    assert [p for p, _ in disposable["client"].calls if p.startswith("/orders/")] == []


def test_disposable_e2e_duplicate_rerun_does_not_duplicate_the_order(disposable):  # noqa: F811
    _export(disposable)
    first = _run(disposable)
    assert first["orders"] == 1

    second = _run(disposable)
    assert second["orders"] == 0
    assert len([p for p, _ in disposable["client"].calls if p == "/orders/preview"]) == 1
    assert len([p for p, _ in disposable["client"].calls if p == "/orders/submit"]) == 1

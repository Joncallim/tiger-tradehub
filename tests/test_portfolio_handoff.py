"""Execution-side PAPER handoff and research-side typed import tests."""

from __future__ import annotations

import json

import pytest

from tradehub.ops.portfolio_handoff import sanitized_paper_portfolio_handoff
from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.handoff import (
    PortfolioHandoffUnavailable,
    load_paper_portfolio_snapshot,
)


def _database(tmp_path) -> ResearchDB:
    database = ResearchDB(tmp_path / "research.db")
    database.migrate()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            ("sec-aapl", "AAPL", "NASDAQ", "Apple", "Tech", None, "SUPPORTED", "2024-01-01", None),
        )
    return database


def _payload(*, positions=None):
    return sanitized_paper_portfolio_handoff(
        account_summary={"asset_value": "110", "cash_balance": "10"},
        paper_proof={
            "environment": "PAPER_SANDBOX",
            "account_type": "PAPER",
            "account_status": "Funded",
        },
        positions=positions
        or [
            {
                "symbol": "AAPL",
                "quantity": "2",
                "available_quantity": "2",
                "market_value": "100",
                "latest_price": "50",
                "currency": "USD",
                "account": "secret",
            }
        ],
        as_of="2026-09-10T00:00:00Z",
    )


def test_execution_handoff_allowlists_no_account_or_credentials():
    handoff = _payload()
    assert handoff["positions"] == [
        {
            "ticker": "AAPL",
            "quantity": "2",
            "sellable_quantity": "2",
            "market_value": "100",
            "mark_price": "50",
            "currency": "USD",
        }
    ]
    assert '"account"' not in json.dumps(handoff)


def test_typed_nonempty_paper_handoff_maps_to_snapshot(tmp_path):
    database = _database(tmp_path)
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(_payload()))
    snapshot = load_paper_portfolio_snapshot(
        database, decision_as_of="2026-09-10T01:00:00Z", pipeline_run_id="run-1", path=path
    )
    assert snapshot.cash_microusd == 10_000_000
    assert snapshot.nav_microusd == 110_000_000
    assert snapshot.holdings[0]["security_id"] == "sec-aapl"
    assert snapshot.holdings[0]["sellable_quantity_microunits"] == 2_000_000
    assert snapshot.market_inputs[0]["mark_price_microusd"] == 50_000_000


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update({"environment": "UNKNOWN"}),
        lambda p: p.update({"as_of": "2026-09-01T00:00:00Z"}),
        lambda p: p.update({"positions": [{"ticker": "AAPL"}]}),
        lambda p: p["positions"][0].update({"market_value": "99"}),
    ],
)
def test_bad_or_stale_handoff_fails_closed(tmp_path, mutate):
    database = _database(tmp_path)
    payload = _payload()
    mutate(payload)
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(PortfolioHandoffUnavailable):
        load_paper_portfolio_snapshot(
            database, decision_as_of="2026-09-10T01:00:00Z", pipeline_run_id="run-1", path=path
        )

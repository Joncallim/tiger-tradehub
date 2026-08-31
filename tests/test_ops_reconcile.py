"""Broker reconciliation contract tests (observation mode)."""

from __future__ import annotations

import json

from tradehub.ops.reconcile import _build_row, _persist


class FakeGateway:
    def proof_paper_environment(self):
        return {
            "environment": "LIVE",
            "account": "A",
            "account_type": "PAPER",
            "account_status": "Funded",
        }

    def get_assets(self):
        return {
            "net_asset_value": 100_000.0,
            "day_pnl": 250.0,
            "available_funds": 40_000.0,
            "gross_position_value": 60_000.0,
            "realized_pnl": 100.0,
            "unrealized_pnl": 150.0,
        }


def test_build_row_maps_broker_assets_and_keeps_missing_null(tmp_path):
    row = _build_row(
        {"net_asset_value": 100_000.0, "day_pnl": 250.0, "available_funds": 40_000.0},
        {"account_type": "PAPER", "account_status": "Funded"},
    )
    assert row["asset_value"] == 100_000.0
    assert row["daily_pnl"] == 250.0
    assert row["cash_balance"] == 40_000.0
    assert row["gross_position_value"] is None  # not reported -> UNKNOWN
    assert row["deposits"] is None  # never fabricated as 0
    assert row["account_type"] == "PAPER"


def test_persist_replaces_same_date_and_appends_history(tmp_path):
    (tmp_path / "analytics").mkdir()
    import tradehub.ops.reconcile as rec

    rec.ANALYTICS_DIR = tmp_path / "analytics"
    rec.HISTORY = rec.ANALYTICS_DIR / "history.jsonl"
    rec.LATEST = rec.ANALYTICS_DIR / "latest.json"

    first = {"date": "2026-08-30", "asset_value": 99_000.0}
    _persist(first)
    later_same_day = {"date": "2026-08-30", "asset_value": 99_500.0}
    _persist(later_same_day)
    next_day = {"date": "2026-08-31", "asset_value": 100_000.0}
    _persist(next_day)

    rows = [json.loads(line) for line in rec.HISTORY.read_text().splitlines() if line.strip()]
    assert len(rows) == 2  # same-date replaced, not duplicated
    assert rows[0]["asset_value"] == 99_500.0
    assert rows[1]["date"] == "2026-08-31"
    latest = json.loads(rec.LATEST.read_text())
    assert latest["date"] == "2026-08-31"


def test_reconcile_roundtrip_with_fake_gateway(tmp_path):
    import tradehub.ops.reconcile as rec

    rec.ANALYTICS_DIR = tmp_path / "analytics"
    rec.HISTORY = rec.ANALYTICS_DIR / "history.jsonl"
    rec.LATEST = rec.ANALYTICS_DIR / "latest.json"
    row = rec.reconcile(FakeGateway())
    assert row["asset_value"] == 100_000.0
    assert row["account_type"] == "PAPER"
    assert rec.LATEST.exists()

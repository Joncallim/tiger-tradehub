"""Acceptance ancestry must not become investment evidence or learning input."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tradehub_research.db import ResearchDB
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.forward_collector import record_prediction_from_screen


class AcceptanceConsumersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _screens(self):
        # Minimal relational fixture: consumer queries run against real SQLite.
        database = ResearchDB(self.root / "screens.db")
        with database.connect() as conn:
            conn.executescript(
                "CREATE TABLE pipeline_run (run_id TEXT, as_of TEXT);"
                "CREATE TABLE screen_definition (config_hash TEXT, family TEXT);"
                "CREATE TABLE screen_result (run_id TEXT, config_hash TEXT, security_id TEXT,"
                "confidence REAL, passed INTEGER, sufficient_data INTEGER);"
            )
            conn.execute("INSERT INTO screen_definition VALUES ('cfg', 'quality')")
            conn.execute("INSERT INTO pipeline_run VALUES ('genuine', '2026-01-05')")
            conn.executemany(
                "INSERT INTO screen_result VALUES ('genuine', 'cfg', ?, ?, 1, 1)",
                [("A", 0.1), ("B", 0.5), ("C", 0.9)],
            )
        return database

    def _add_acceptance(self, database):
        with database.connect() as conn:
            conn.executemany(
                "INSERT INTO pipeline_run VALUES (?, '2026-01-05')",
                [("pr66-acceptance-newest",), ("",), (None,)],
            )
            conn.executemany(
                "INSERT INTO screen_result VALUES (?, 'cfg', ?, ?, 1, 1)",
                [
                    (run, sid, score)
                    for run in ("pr66-acceptance-newest", "", None)
                    for sid, score in (("A", 1.0), ("B", 0.5), ("C", 0.0))
                ],
            )

    def _screen(self, run_id):
        return {
            "run_id": run_id,
            "security_id": "synthetic",
            "as_of": "2026-01-05",
            "confidence": 0.9,
            "passed": True,
            "sufficient_data": True,
            "raw_features_json": "{}",
            "evidence_ids_json": "[]",
            "config_hash": "cfg",
        }

    def test_evaluation_inputs_exclude_acceptance_and_keep_history(self):
        from tradehub_research.validation.replay import load_screen_results

        database = self._screens()
        genuine = load_screen_results(database)
        self._add_acceptance(database)
        with database.connect(read_only=True) as conn:
            before = list(conn.iterdump())
        self.assertEqual(load_screen_results(database), genuine)
        with database.connect(read_only=True) as conn:
            self.assertEqual(list(conn.iterdump()), before)
            self.assertEqual(conn.execute("SELECT count(*) FROM screen_result").fetchone()[0], 12)

    def test_investment_conclusion_unchanged_by_conflicting_acceptance_scores(self):
        from tradehub_research.validation.baselines import (
            compute_ic_horizon,
            group_screens_by_date_security,
            signals_for_baseline,
        )
        from tradehub_research.validation.replay import load_screen_results

        database = self._screens()
        # Three genuine dates produce a numeric conclusion, so synthetic
        # scores must not merely preserve an INSUFFICIENT_DATA verdict.
        with database.connect() as conn:
            for day in ("2026-01-06", "2026-01-07"):
                conn.execute("INSERT INTO pipeline_run VALUES (?, ?)", (day, day))
                conn.executemany(
                    "INSERT INTO screen_result VALUES (?, 'cfg', ?, ?, 1, 1)",
                    [(day, sid, score) for sid, score in (("A", 0.1), ("B", 0.5), ("C", 0.9))],
                )
        outcomes = {
            sid: dict.fromkeys(("2026-01-05", "2026-01-06", "2026-01-07"), value)
            for sid, value in (("A", -0.1), ("B", 0.0), ("C", 0.1))
        }

        def conclusion():
            grouped = group_screens_by_date_security(load_screen_results(database))
            signals = signals_for_baseline("B4_EQUAL_SCORING", grouped)
            return signals, compute_ic_horizon(signals, outcomes, 21, 69)

        genuine = conclusion()
        self._add_acceptance(database)
        self.assertEqual(conclusion(), genuine)
        self.assertEqual(genuine[1]["mean_ic"], 1.0)
        self.assertEqual(genuine[1]["date_count"], 3)

    def test_production_ingress_rejects_known_acceptance_before_any_write(self):
        database = ExperimentDB(self.root / "experiment.db")
        database.migrate()
        for run in ("pr66-acceptance-0", "pr66-acceptance-v2-3", "", None):
            with self.subTest(run=run), self.assertRaisesRegex(ValueError, "acceptance"):
                record_prediction_from_screen(
                    database,
                    screen=self._screen(run),
                    horizon_sessions=21,
                    collection_date=date(2026, 1, 5),
                )
        with database.connect(read_only=True) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM forward_prediction").fetchone()[0], 0
            )

    def test_rejected_screen_preserves_existing_append_only_history(self):
        database = ExperimentDB(self.root / "experiment.db")
        database.migrate()
        from tradehub_research.validation.forward_collector import record_prediction

        prediction = record_prediction(
            database,
            security_id="S",
            as_of="2026-01-05",
            variant_name="production/quality",
            score_value=0.5,
            state=None,
            screen_passed=True,
            sufficient_data=True,
            raw_features={},
            config_hash="cfg",
            evidence_ids=[],
            horizon_sessions=21,
            collection_date=date(2026, 1, 5),
        )
        with database.connect(read_only=True) as conn:
            before = tuple(conn.execute("SELECT * FROM forward_prediction").fetchone())
        with self.assertRaisesRegex(ValueError, "acceptance"):
            record_prediction_from_screen(
                database,
                screen=self._screen("pr66-acceptance-0"),
                horizon_sessions=21,
                collection_date=date(2026, 1, 5),
            )
        with database.connect(read_only=True) as conn:
            self.assertEqual(
                tuple(conn.execute("SELECT * FROM forward_prediction").fetchone()), before
            )
        for statement in (
            "DELETE FROM forward_prediction WHERE prediction_id=?",
            "UPDATE forward_prediction SET score_value=1 WHERE prediction_id=?",
        ):
            with self.assertRaises(sqlite3.IntegrityError), database.connect() as conn:
                conn.execute(statement, (prediction,))

    def test_portfolio_reports_use_broker_values_despite_synthetic_decisions(self):
        from tradehub_research.ops import report_cli

        database = self._screens()
        with database.connect() as conn:
            conn.executescript(
                "CREATE TABLE portfolio_run (run_id TEXT, pipeline_run_id TEXT);"
                "CREATE TABLE portfolio_state_observation (run_id TEXT, conviction REAL);"
                "INSERT INTO portfolio_run VALUES ('real', 'genuine');"
                "INSERT INTO portfolio_state_observation VALUES ('real', 10);"
            )
        paths = SimpleNamespace(research_db=database.path)
        broker = {
            "asset_value": 1100,
            "daily_pnl": 7,
            "daily_pnl_pct": 0.64,
            "realized_pnl": 3,
            "unrealized_pnl": 4,
            "cash_balance": 500,
        }
        history = [
            {"date": "2026-01-05", "asset_value": 1000, "deposits": 0, "withdrawals": 0},
            {"date": "2026-01-12", "asset_value": 1100, "deposits": 0, "withdrawals": 0},
        ]
        # Isolate external operational inputs; exercise both real report builders.
        with (
            patch.object(report_cli, "forward_health", return_value={}),
            patch.object(report_cli, "refresh_health", return_value={}),
            patch.object(report_cli, "_freshness_view", return_value=({}, None)),
            patch.object(report_cli, "_decision_plane_note", return_value=None),
            patch.object(report_cli, "LEDGER", self.root / "absent.jsonl"),
        ):

            def reports():
                args = {
                    "settings": SimpleNamespace(),
                    "experiment_db": None,
                    "paths": paths,
                    "analytics": broker,
                }
                return (
                    report_cli.build_daily_report(**args),
                    report_cli.build_weekly_report(**args, history=history, benchmark_pct=2),
                )

            before = reports()
            with database.connect() as conn:
                conn.execute("INSERT INTO portfolio_run VALUES ('fake', 'pr66-acceptance-0')")
                conn.execute("INSERT INTO portfolio_state_observation VALUES ('fake', 999999)")
            self.assertEqual(reports(), before)
            self.assertIn("Today: +$7.00", before[0])
            self.assertIn("Week: +$100.00 (+10.00%)", before[1])


if __name__ == "__main__":
    unittest.main()

"""Deterministic separation of acceptance rows from genuine production rows.

The synthetic acceptance pipelines are durable and append-only, so they cannot
be removed. Every reporting/learning surface must instead exclude them, and the
operator's genuine-production counts must never include them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tradehub_research.ops.acceptance_rows import (
    ACCEPTANCE_RUN_PREFIXES,
    acceptance_clause,
    genuine_clause,
    is_acceptance_run,
)

ROOT = Path(__file__).resolve().parents[1]


def test_acceptance_prefixes_are_explicit():
    assert "pr66-acceptance-" in ACCEPTANCE_RUN_PREFIXES


def test_is_acceptance_run_detects_labelled_and_unattributable_runs():
    assert is_acceptance_run("pr66-acceptance-0") is True
    assert is_acceptance_run("pr66-acceptance-v2-3") is True
    assert (
        is_acceptance_run("bcb73591996077de45a9c5e6ea8d72be2dd3781d870e999dbd9809bde83f5ef8")
        is False
    )
    # Unattributable rows are never counted as genuine production.
    assert is_acceptance_run(None) is True
    assert is_acceptance_run("") is True


def test_sql_clauses_are_complementary(tmp_path):
    con = sqlite3.connect(tmp_path / "t.db")
    con.execute("CREATE TABLE r (pipeline_run_id TEXT)")
    rows = [
        "pr66-acceptance-0",
        "pr66-acceptance-v2-1",
        "bcb73591996077de45a9c5e6ea8d72be2dd3781d870e999dbd9809bde83f5ef8",
        "some-other-production-run",
    ]
    con.executemany("INSERT INTO r VALUES (?)", [(r,) for r in rows])

    gsql, gparams = genuine_clause()
    asql, aparams = acceptance_clause()
    genuine = {r[0] for r in con.execute(f"SELECT pipeline_run_id FROM r WHERE {gsql}", gparams)}
    acceptance = {r[0] for r in con.execute(f"SELECT pipeline_run_id FROM r WHERE {asql}", aparams)}
    assert genuine == {
        "bcb73591996077de45a9c5e6ea8d72be2dd3781d870e999dbd9809bde83f5ef8",
        "some-other-production-run",
    }
    assert acceptance == {"pr66-acceptance-0", "pr66-acceptance-v2-1"}
    # Partition: every row is exactly one of the two.
    assert genuine | acceptance == set(rows)
    assert genuine & acceptance == set()


def test_operator_status_excludes_acceptance_rows_and_reports_the_split():
    src = (ROOT / "tradehub_research" / "ops" / "operator_status.py").read_text(encoding="utf-8")
    # Genuine counts must be filtered by the shared clause...
    assert "genuine_clause()" in src
    assert 'WHERE " + gsql' in src or '" WHERE " + gsql' in src
    # ...and the split must be reported explicitly.
    assert '"provenance"' in src
    assert "acceptance_run_prefixes" in src
    # The latest decision must not be an acceptance run.
    assert "FROM portfolio_run WHERE " in src

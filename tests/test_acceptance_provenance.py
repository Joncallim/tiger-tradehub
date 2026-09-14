"""Deterministic separation of acceptance rows from genuine production rows.

The synthetic acceptance pipelines are durable and append-only, so they cannot
be removed. Every reporting/learning surface must instead exclude them, and the
operator's genuine-production counts must never include them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


def _mini_db(tmp_path):
    """Minimal DB with the shapes operator_status queries."""
    con = sqlite3.connect(tmp_path / "mini.db")
    con.execute(
        "CREATE TABLE portfolio_run (run_id TEXT, pipeline_run_id TEXT, "
        "decision_as_of TEXT, created_at TEXT)"
    )
    con.execute("CREATE TABLE portfolio_state_observation (run_id TEXT)")
    con.executemany(
        "INSERT INTO portfolio_run VALUES (?,?,?,?)",
        [
            ("gen-1", "bcb73591996077de45a9c5e6ea8d72be2dd3781d870e999dbd9809bde83f5ef8", "t", "2"),
            ("gen-2", "another-genuine-run", "t", "1"),
            ("acc-1", "pr66-acceptance-0", "t", "4"),
            ("acc-2", "pr66-acceptance-v2-3", "t", "3"),
        ],
    )
    con.executemany(
        "INSERT INTO portfolio_state_observation VALUES (?)",
        [("gen-1",), ("gen-2",), ("acc-1",), ("acc-2",)],
    )
    con.commit()
    return con


def test_operator_status_queries_separate_genuine_from_acceptance(tmp_path):
    """Behavioural: run the SAME query shapes operator_status composes."""
    con = _mini_db(tmp_path)
    gsql, gparams = genuine_clause()

    genuine_runs = con.execute(
        f"SELECT count(*) FROM portfolio_run WHERE {gsql}", gparams
    ).fetchone()[0]
    all_runs = con.execute("SELECT count(*) FROM portfolio_run").fetchone()[0]
    genuine_obs = con.execute(
        "SELECT count(*) FROM portfolio_state_observation o JOIN portfolio_run r "
        f"ON r.run_id = o.run_id WHERE r.{gsql}",
        gparams,
    ).fetchone()[0]

    assert genuine_runs == 2
    assert all_runs == 4
    assert all_runs - genuine_runs == 2  # acceptance rows excluded
    assert genuine_obs == 2

    # The latest GENUINE decision must never be an acceptance run.
    latest = con.execute(
        f"SELECT pipeline_run_id FROM portfolio_run WHERE {gsql} ORDER BY created_at DESC LIMIT 1",
        gparams,
    ).fetchone()[0]
    assert not is_acceptance_run(latest)
    assert latest == "bcb73591996077de45a9c5e6ea8d72be2dd3781d870e999dbd9809bde83f5ef8"


def test_operator_status_source_wires_the_shared_clause_and_reports_the_split():
    src = (ROOT / "tradehub_research" / "ops" / "operator_status.py").read_text(encoding="utf-8")
    assert "genuine_clause()" in src
    assert '"provenance"' in src
    assert "acceptance_run_prefixes" in src
    # Raw grand totals stay visible rather than being silently rewritten.
    assert "portfolio_runs_all_total" in src
    assert "observations_all_total" in src


def test_forward_capture_selects_the_latest_genuine_run_behaviourally(tmp_path):
    """Executes the REAL selection query against a schema-accurate pipeline_run.

    pipeline_run's own identity column is ``run_id`` (it has NO pipeline_run_id
    column), so the genuine filter must be built with column="run_id". A
    default-column filter here throws OperationalError: no such column, which
    would break every unattended capture run.
    """
    con = sqlite3.connect(tmp_path / "capture.db")
    con.execute("CREATE TABLE pipeline_run (run_id TEXT, as_of TEXT, started_at TEXT)")
    con.executemany(
        "INSERT INTO pipeline_run VALUES (?,?,?)",
        [
            ("genuine-old", "2026-09-08", "2026-09-08T00:00:00Z"),
            ("pr66-acceptance-0", "2026-09-14", "2026-09-14T10:00:00Z"),
            ("pr66-acceptance-v2-3", "2026-09-14", "2026-09-14T11:00:00Z"),
            ("genuine-new", "2026-09-10", "2026-09-10T00:00:00Z"),
        ],
    )
    con.commit()

    gsql, gparams = genuine_clause(column="run_id")
    picked = con.execute(
        f"SELECT run_id FROM pipeline_run WHERE {gsql} ORDER BY started_at DESC LIMIT 1",
        gparams,
    ).fetchone()[0]
    assert picked == "genuine-new", "must skip the newer acceptance runs"
    assert not is_acceptance_run(picked)

    # The default column would NOT work against this table at all.
    bad_sql, bad_params = genuine_clause()
    with pytest.raises(sqlite3.OperationalError):
        con.execute(
            f"SELECT run_id FROM pipeline_run WHERE {bad_sql} LIMIT 1", bad_params
        ).fetchone()


def test_forward_capture_uses_the_run_id_column_for_pipeline_run():
    """Source guard: the pipeline_run filter must not use the default column."""
    src = (ROOT / "tradehub_research" / "ops" / "forward_capture.py").read_text(encoding="utf-8")
    assert 'genuine_clause(column="run_id")' in src
    assert "is_acceptance_run" in src
    assert "SKIPPED_ACCEPTANCE_RUN" in src


def test_provenance_queries_use_the_right_column_per_table(tmp_path):
    """Regression for the malformed-SQL finding.

    A schema-accurate DB: score_snapshot has NEITHER pipeline_run_id NOR run_id
    (it links via committee_run), and portfolio_state_observation has no
    pipeline_run_id (it links via portfolio_run.run_id). Every provenance query
    must therefore use an explicitly qualified column, and must NOT silently
    degrade to zeros.
    """
    con = sqlite3.connect(tmp_path / "prov.db")
    con.execute("CREATE TABLE portfolio_run (run_id TEXT, pipeline_run_id TEXT, created_at TEXT)")
    con.execute("CREATE TABLE portfolio_state_observation (decision_id TEXT, run_id TEXT)")
    con.execute("CREATE TABLE committee_run (committee_run_id TEXT, pipeline_run_id TEXT)")
    con.execute("CREATE TABLE score_snapshot (snapshot_id TEXT, committee_run_id TEXT)")

    con.executemany(
        "INSERT INTO portfolio_run VALUES (?,?,?)",
        [("r1", "genuine-a", "2"), ("r2", "pr66-acceptance-0", "1")],
    )
    con.executemany(
        "INSERT INTO portfolio_state_observation VALUES (?,?)",
        [("d1", "r1"), ("d2", "r2")],
    )
    con.executemany(
        "INSERT INTO committee_run VALUES (?,?)",
        [("c1", "genuine-a"), ("c2", "pr66-acceptance-0"), ("c3", "genuine-b")],
    )
    con.executemany(
        "INSERT INTO score_snapshot VALUES (?,?)",
        [("s1", "c1"), ("s2", "c2"), ("s3", "c3")],
    )
    con.commit()

    gsql, gparams = genuine_clause()
    obs_sql, obs_params = genuine_clause(column="r.pipeline_run_id")
    committee_sql, committee_params = genuine_clause(column="c.pipeline_run_id")

    specs = (
        ("portfolio_runs", "portfolio_run", "SELECT count(*) FROM portfolio_run", gsql, gparams),
        (
            "observations",
            "portfolio_state_observation o JOIN portfolio_run r ON r.run_id = o.run_id",
            "SELECT count(*) FROM portfolio_state_observation",
            obs_sql,
            obs_params,
        ),
        (
            "score_snapshots",
            "score_snapshot s JOIN committee_run c ON c.committee_run_id = s.committee_run_id",
            "SELECT count(*) FROM score_snapshot",
            committee_sql,
            committee_params,
        ),
        ("committee_runs", "committee_run", "SELECT count(*) FROM committee_run", gsql, gparams),
    )
    results = {}
    for key, source, total_sql, where_sql, where_params in specs:
        try:
            total = con.execute(total_sql).fetchone()[0]
            genuine = con.execute(
                f"SELECT count(*) FROM {source} WHERE {where_sql}", where_params
            ).fetchone()[0]
            results[key] = {"genuine": genuine, "acceptance": total - genuine}
        except sqlite3.Error as exc:  # pragma: no cover - would be the defect
            results[key] = {"error": f"{type(exc).__name__}: {exc}"}

    assert results == {
        "portfolio_runs": {"genuine": 1, "acceptance": 1},
        "observations": {"genuine": 1, "acceptance": 1},
        "score_snapshots": {"genuine": 2, "acceptance": 1},
        "committee_runs": {"genuine": 2, "acceptance": 1},
    }, results

    # The naive form (unqualified / wrong column) genuinely fails — which is
    # exactly the silent-zero defect this test guards against.
    with pytest.raises(sqlite3.OperationalError):
        con.execute("SELECT count(*) FROM score_snapshot WHERE not_a_real_column").fetchone()


def test_operator_status_provenance_is_not_silently_swallowed():
    src = (ROOT / "tradehub_research" / "ops" / "operator_status.py").read_text(encoding="utf-8")
    # Failures must be RECORDED, not swallowed by a bare pass.
    assert "query_errors" in src
    assert 'provenance[key] = {"genuine": None, "acceptance": None}' in src
    # Every provenance query must be explicitly column-qualified.
    assert 'genuine_clause(column="r.pipeline_run_id")' in src
    assert 'genuine_clause(column="c.pipeline_run_id")' in src


def test_all_total_is_none_safe_and_never_fakes_a_zero():
    """The exact defect: None + None crashed the whole status surface."""
    from tradehub_research.ops.operator_status import _all_total

    assert _all_total({"genuine": None, "acceptance": None}) is None
    assert _all_total({"genuine": 1, "acceptance": 2}) == 3
    assert _all_total({"genuine": 0, "acceptance": 0}) == 0
    assert _all_total({}) is None
    assert _all_total(None) is None
    # Must never raise for any malformed shape.
    assert _all_total({"genuine": None, "acceptance": 5}) is None


def test_operator_status_survives_a_failing_provenance_query(tmp_path, monkeypatch):
    """ONE failing provenance query must RECORD the error and still return fully.

    Regression for the crash: with the score_snapshot split unknown, that table's
    counts must be None and the status payload must still be produced in full —
    not an unhandled TypeError, and not a fake 0. Every other provenance query
    keeps working, which is the realistic failure mode.
    """
    import sqlite3 as _sqlite3
    from types import SimpleNamespace

    from tradehub_research.ops import operator_status as ops

    real = _sqlite3.connect(tmp_path / "s.db")
    real.row_factory = _sqlite3.Row  # operator_status reads rows by column name
    real.execute("CREATE TABLE pipeline_run (run_id TEXT, as_of TEXT, started_at TEXT)")
    real.execute(
        "CREATE TABLE portfolio_run (run_id TEXT, pipeline_run_id TEXT, "
        "decision_as_of TEXT, created_at TEXT)"
    )
    real.execute("CREATE TABLE portfolio_state_observation (decision_id TEXT, run_id TEXT)")
    real.execute("CREATE TABLE committee_run (committee_run_id TEXT, pipeline_run_id TEXT)")
    real.execute("CREATE TABLE score_snapshot (snapshot_id TEXT, committee_run_id TEXT)")
    real.execute(
        "CREATE TABLE trade_proposal (proposal_id TEXT, decision_id TEXT, export_status TEXT)"
    )
    real.execute(
        "INSERT INTO portfolio_run VALUES ('r1','genuine-a','2026-09-10T00:00:00Z','2026-09-10')"
    )
    real.commit()

    class _Conn:
        def execute(self, sql, params=()):
            if "score_snapshot" in sql:
                raise _sqlite3.OperationalError("simulated: score_snapshot query failed")
            return real.execute(sql, params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            pass

    class _DB:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            return _Conn()

    class _EmptyCursor:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _EmptyConn:
        def execute(self, sql, params=()):
            return _EmptyCursor()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _ExpDB:
        def connect(self, *args, **kwargs):
            return _EmptyConn()

    monkeypatch.setattr(ops, "ResearchDB", _DB)
    monkeypatch.setattr(
        ops,
        "refresh_health",
        lambda **kwargs: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    monkeypatch.setattr(
        ops,
        "forward_health",
        lambda **kwargs: {
            "production_predictions": 0,
            "predictions_due": 0,
            "matured": {},
        },
    )
    monkeypatch.setattr(ops, "_authority_record_count", lambda: 0)
    monkeypatch.setattr(ops, "_runner_receipts", lambda: {"count": 0})
    monkeypatch.setattr(ops, "_published_authority_ids", lambda: set())

    paths = SimpleNamespace(
        research_db=tmp_path / "r.db",
        experiment_db=tmp_path / "e.db",
        research_dir=tmp_path,
    )
    settings = SimpleNamespace(busy_timeout_ms=5000)

    out = ops.operator_status(settings=settings, experiment_db=_ExpDB(), paths=paths)

    assert isinstance(out, dict) and out, "operator_status must still return a payload"
    chain = out["decision_chain"]
    provenance = chain["provenance"]
    # The ONE failed query is recorded, not swallowed...
    assert "score_snapshots" in provenance["query_errors"], provenance
    # ...its split is honestly unknown...
    assert provenance["score_snapshots"]["genuine"] is None
    # ...the healthy queries still report, and nothing crashed.
    assert provenance["portfolio_runs"]["genuine"] == 1
    assert chain["provenance"]["query_errors"]["score_snapshots"].startswith("OperationalError")


def test_decision_ledger_totals_are_none_not_a_silent_zero_on_failure(tmp_path, monkeypatch):
    """The sibling-field defect: a broken ledger query must not read as 'empty system'.

    `portfolio_runs_total`/`observations_total` must be None (not the 0 default)
    and the failure recorded, so an operator can tell 'no production yet' apart
    from 'the status query is broken'.
    """
    import sqlite3 as _sqlite3
    from types import SimpleNamespace

    from tradehub_research.ops import operator_status as ops

    class _BoomConn:
        def execute(self, sql, params=()):
            raise _sqlite3.OperationalError("simulated: ledger query failed")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _BoomDB:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            return _BoomConn()

    class _EmptyCursor:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class _EmptyConn:
        def execute(self, sql, params=()):
            return _EmptyCursor()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _ExpDB:
        def connect(self, *args, **kwargs):
            return _EmptyConn()

    monkeypatch.setattr(ops, "ResearchDB", _BoomDB)
    monkeypatch.setattr(
        ops,
        "refresh_health",
        lambda **kwargs: {"securities_expected": 0, "with_bars": 0, "stale_count": 0},
    )
    monkeypatch.setattr(
        ops,
        "forward_health",
        lambda **kwargs: {"production_predictions": 0, "predictions_due": 0, "matured": {}},
    )
    monkeypatch.setattr(ops, "_authority_record_count", lambda: 0)
    monkeypatch.setattr(ops, "_runner_receipts", lambda: {"count": 0})
    monkeypatch.setattr(ops, "_published_authority_ids", lambda: set())

    paths = SimpleNamespace(
        research_db=tmp_path / "r.db",
        experiment_db=tmp_path / "e.db",
        research_dir=tmp_path,
    )
    out = ops.operator_status(
        settings=SimpleNamespace(busy_timeout_ms=5000), experiment_db=_ExpDB(), paths=paths
    )

    chain = out["decision_chain"]
    # NOT a silent 0 that looks like an empty system.
    assert chain["portfolio_runs_total"] is None, chain
    assert chain["observations_total"] is None, chain
    # And the failure is recorded, not swallowed.
    errors = chain["query_errors"]
    assert "portfolio_runs_total" in errors and "observations_total" in errors, errors
    assert errors["portfolio_runs_total"].startswith("OperationalError")
    # The payload is still complete.
    assert out.get("report_status") is not None


def test_null_rows_partition_into_acceptance_not_genuine(tmp_path):
    """A NULL/unattributable run id is never genuine production."""
    con = sqlite3.connect(tmp_path / "nulls.db")
    con.execute("CREATE TABLE r (pipeline_run_id TEXT)")
    con.executemany("INSERT INTO r VALUES (?)", [(None,), ("",), ("genuine-run",)])
    gsql, gparams = genuine_clause()
    asql, aparams = acceptance_clause()
    genuine = {r[0] for r in con.execute(f"SELECT pipeline_run_id FROM r WHERE {gsql}", gparams)}
    acceptance = {r[0] for r in con.execute(f"SELECT pipeline_run_id FROM r WHERE {asql}", aparams)}
    assert genuine == {"genuine-run"}
    assert acceptance == {None, ""}
    assert len(genuine) + len(acceptance) == 3

"""Regression test: rotation totals must not absorb active-set work.

Codex review round 5 (P2). ``RefreshRunStore.finish()`` derived ``candidates``
from ``len(outcomes)`` and ``refreshed`` from every ``REFRESHED`` outcome, so an
active-set symbol that refreshed successfully was counted as a rotation candidate
served. With active work plus a full rotation the durable record (and therefore
the report that quotes it) claimed more work than the rotation budget permits,
and a demand the rotation never had.

The record keeps two distinct things: the per-symbol outcomes (everything the run
touched, active phase included -- the diagnosis reads those) and the ROTATION
totals (what the budget was spent on).

Offline and deterministic.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from tradehub_research.db import ResearchDB
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import refresh_runs
from tradehub_research.validation.experiment_db import ExperimentDB

AS_OF = date(2026, 9, 18)


def _run(tmp_path, monkeypatch):
    bars = {"S-ACT": "2026-08-01", "S-Z1": "2026-06-01", "S-Z2": "2026-07-01"}
    ResearchDB(tmp_path / "research.db", 5000).migrate()
    experiment_db = ExperimentDB(tmp_path / "experiment.db")
    experiment_db.migrate()

    monkeypatch.setattr(dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars})
    monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
    monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
    monkeypatch.setattr(dr, "_load_retired", set)
    monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: {"ACT"})
    monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, tickers: {})

    attempted: list[str] = []

    def fake_refresh_one(_a, _q, _r, _e, _s, ticker, as_of, summary):
        attempted.append(ticker)
        summary["SUCCESS"] += 1
        bars[f"S-{ticker}"] = as_of.isoformat()  # every fetch lands

    monkeypatch.setattr(dr, "_refresh_one", fake_refresh_one)

    class _Quota:
        def bootstrap_usage(self, _now, _limit):
            return {"used": 0, "symbols": []}

    monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))
    settings = SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token=None,
        tiingo_license_confirmed=True,
        adapter_cache_dir=tmp_path,
    )
    paths = SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)
    summary = dr.run_daily_refresh(
        settings=settings,
        experiment_db=experiment_db,
        paths=paths,
        as_of=AS_OF,
        rotation_budget=1,  # one rotation request: Z1 served, Z2 deferred
    )
    return summary, attempted, paths


def test_active_work_is_not_counted_as_rotation_work(tmp_path, monkeypatch):
    summary, attempted, paths = _run(tmp_path, monkeypatch)
    # ACT refreshes in the active phase; then the rotation spends its single
    # request on the worst remaining candidate.
    assert attempted[:1] == ["ACT"]
    assert sorted(attempted[1:]) == ["Z1"]

    store = refresh_runs.store_for(paths)
    record = store.run(AS_OF.isoformat())
    assert record["candidates"] == 2, (
        "rotation demand excludes the active symbol: Z1 + Z2, not len(outcomes)"
    )
    assert record["refreshed"] == 1, "one rotation request was available"
    assert record["deferred"] == 1, "Z2 waits for the next run"
    # The per-symbol outcomes still cover everything the run touched.
    outcomes = store.outcomes(AS_OF.isoformat())
    assert outcomes["ACT"] == refresh_runs.REFRESHED
    assert outcomes["Z1"] == refresh_runs.REFRESHED
    assert outcomes["Z2"] == refresh_runs.DEFERRED_BUDGET
    assert summary["refresh_run_deferred"] == 1


def test_the_demand_never_exceeds_what_the_rotation_could_hold(tmp_path, monkeypatch):
    """The reported served-over-demand can never exceed the budget."""
    _summary, _attempted, paths = _run(tmp_path, monkeypatch)
    record = refresh_runs.store_for(paths).run(AS_OF.isoformat())
    assert record["refreshed"] <= record["rotation_budget"]
    assert record["candidates"] >= record["refreshed"]

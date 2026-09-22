"""Regression tests: the sixth Codex review round on PR #74.

Three follow-up P2 findings against ``3e7fe7b``, each a second-order defect of the
previous round's fixes:

A. ``rotation_refreshed`` counts *attempts* (a failed or empty fetch still spends a
   request), and it was written into the durable ``refreshed`` column — so the
   operator report claimed the rotation had served candidates that actually
   failed. The record must count successes.
B. An active-set **failure** stays stale, so it reappeared in
   ``rotation_candidates`` and was counted as rotation demand the budget was
   measured against, even though the rotation never had it to serve.
C. The cooling pool could only spend *leftover* budget. With ready demand
   permanently at capacity (steady-state arrivals at the computed rotation
   capacity) it never got a slot at all, and because scheduled deferrals are not
   remediated, a transiently failing symbol could stay quarantined forever after
   the provider recovered. Cooling now gets a reserved share — but never the last
   slot, so a one-request budget still goes to the healthy candidate.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from tradehub_research.db import ResearchDB
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import refresh_runs
from tradehub_research.validation.experiment_db import ExperimentDB

AS_OF = date(2026, 9, 18)
WINDOW = 6


def _failed(streak: int = 1, last: str = "2026-09-17T00:00:00+00:00") -> dict:
    return {"streak": streak, "last_attempt_at": last}


# ---------------------------------------------------------------------------
# C -- the cooling share is reserved, not merely leftover
# ---------------------------------------------------------------------------
class TestCoolingRetriesAreReserved:
    def test_a_permanently_saturated_ready_pool_still_retries_a_cooling_symbol(self):
        """The failure scenario: ready demand >= budget every run, forever.

        Under leftover-only allocation this returns nothing for cooling, so a
        symbol that failed once is deferred (and, being scheduled, never
        remediated) indefinitely.
        """
        ready = [f"R{i:02d}" for i in range(12)]
        to_attempt, deferrals = dr.allocate_rotation(
            [*ready, "ZCOOL"], budget=4, failures={"ZCOOL": _failed(1)}
        )
        assert "ZCOOL" in to_attempt, "a recovered symbol must be retried, not starved"
        assert len(to_attempt) == 4, "and the budget is still fully spent"
        assert len([t for t in to_attempt if t.startswith("R")]) == 3

    def test_the_reservation_never_takes_the_last_slot_from_ready_work(self):
        """A one-request budget still belongs to the healthy candidate."""
        to_attempt, _deferrals = dr.allocate_rotation(
            ["AHEALTHY"], budget=1, failures={"ZCOOL": _failed(2)}
        )
        assert to_attempt == ["AHEALTHY"]

    def test_the_reserved_share_stays_bounded(self):
        """A cohort of failures cannot take more than its share."""
        cooling = {f"F{i}": _failed(3) for i in range(20)}
        to_attempt, _deferrals = dr.allocate_rotation(
            [f"F{i}" for i in range(20)], budget=20, failures=cooling
        )
        assert len(to_attempt) == max(1, 20 // dr.COOLING_BUDGET_DIVISOR)

    def test_a_few_ready_candidates_do_not_starve_the_cooling_allowance(self):
        """Ready work takes what it needs; cooling still gets its full share."""
        to_attempt, _deferrals = dr.allocate_rotation(
            ["A1", "A2", "ZCOOL", "YCOOL"],
            budget=8,
            failures={"ZCOOL": _failed(), "YCOOL": _failed()},
        )
        assert sorted(to_attempt) == ["A1", "A2", "YCOOL", "ZCOOL"]
        assert len(to_attempt) == 2 + 8 // dr.COOLING_BUDGET_DIVISOR

    def test_a_run_with_nothing_cooling_spends_its_whole_budget_on_ready_work(self):
        """The reservation must not eat slots when nothing is cooling."""
        to_attempt, deferrals = dr.allocate_rotation(["A1", "A2", "A3"], budget=2, failures={})
        assert to_attempt == ["A1", "A2"]
        assert deferrals == {"A3": dr.refresh_runs.DEFERRED_BUDGET}

    def test_cooling_alone_may_use_its_allowance(self):
        """With no ready candidates there is nothing better to spend on."""
        to_attempt, _deferrals = dr.allocate_rotation(
            ["Z1", "Z2", "Z3"], budget=4, failures={f"Z{i}": _failed() for i in (1, 2, 3)}
        )
        assert len(to_attempt) == max(1, 4 // dr.COOLING_BUDGET_DIVISOR)


# ---------------------------------------------------------------------------
# A + B -- the durable totals
# ---------------------------------------------------------------------------
class TestDurableRotationTotals:
    def _run(self, tmp_path, monkeypatch, active_fails: bool, active: bool = True):
        bars = {
            "S-ACT": "2026-08-01",
            "S-Z1": "2026-06-01",
            "S-Z2": "2026-07-01",
            "S-Z3": "2026-05-01",
        }
        ResearchDB(tmp_path / "research.db", 5000).migrate()
        experiment_db = ExperimentDB(tmp_path / "experiment.db")
        experiment_db.migrate()

        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(
            dr, "_active_securities", lambda _db, days=14: {"ACT"} if active else set()
        )
        monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, tickers: {})

        attempted: list[str] = []

        def fake_refresh_one(_a, _q, _r, _e, _s, ticker, as_of, summary):
            attempted.append(ticker)
            if ticker == "ACT" and active_fails:
                summary["ERROR"] += 1
                return  # the active fetch fails; the bar stays stale
            if ticker == "Z1":
                summary["ERROR"] += 1  # the rotation request is spent but fails
                return
            summary["SUCCESS"] += 1
            bars[f"S-{ticker}"] = as_of.isoformat()

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
            rotation_budget=2,  # Z1 attempted (fails), Z2 served, Z3 deferred
        )
        return summary, attempted, refresh_runs.store_for(paths).run(AS_OF.isoformat())

    def test_failed_rotation_attempts_are_not_reported_as_served(self, tmp_path, monkeypatch):
        """A: the report's 'served' figure counts successes, not requests spent."""
        summary, attempted, record = self._run(tmp_path, monkeypatch, active_fails=False)
        assert attempted.count("Z1") == 1, "the failing request was made"
        assert summary["rotation_refreshed"] == 2, "…and it spent one of the two requests"
        assert record["refreshed"] == 1, "but only one candidate actually advanced"
        assert record["candidates"] == 3
        assert record["deferred"] == 1

    def test_an_active_failure_is_not_counted_as_rotation_demand(self, tmp_path, monkeypatch):
        """B: the active symbol never entered the rotation, so it is not demand."""
        _summary, attempted, record = self._run(tmp_path, monkeypatch, active_fails=True)
        assert attempted[0] == "ACT"
        assert "ACT" not in attempted[1:], "no second attempt for the same run"
        outcomes = refresh_runs.store_for(SimpleNamespace(research_dir=tmp_path)).outcomes(
            AS_OF.isoformat()
        )
        assert outcomes["ACT"] == refresh_runs.FAILED
        # Demand is the rotation's own candidate set: Z1, Z2, Z3 -- not ACT.
        assert record["candidates"] == 3

    def test_an_active_success_keeps_the_demand_clean_too(self, tmp_path, monkeypatch):
        _summary, _attempted, record = self._run(tmp_path, monkeypatch, active_fails=False)
        assert record["candidates"] == 3

    def test_the_invariant_the_report_relies_on(self, tmp_path, monkeypatch):
        """served successes can never exceed the budget the rotation was given."""
        _summary, _attempted, record = self._run(tmp_path, monkeypatch, active_fails=True)
        assert record["refreshed"] <= record["rotation_budget"]
        assert record["refreshed"] <= record["candidates"]

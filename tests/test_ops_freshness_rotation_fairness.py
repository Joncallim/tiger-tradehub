"""Regression tests: the rotation must not be monopolised by failing symbols.

Codex review finding (P2, PR #74): with candidates ordered worst-stale-first, a
symbol whose fetch keeps failing never advances its bar, so it returns at the
front of the queue on every run and consumes the budget again. A cohort of such
symbols as large as the budget therefore starves every healthy stale symbol
behind it, indefinitely.

The mechanism under test is a **fairness slice**, not a re-ordering:

* failure state is read from the append-only attempt ledger (the same authority
  the diagnosis classifies from) -- no second retry system;
* ready candidates claim the budget first, so a failing symbol can never take a
  slot from one that might heal;
* failing ("cooling") candidates then claim at most ``budget // 4`` of what is
  left, least-recently-attempted first, so they are still retried and rotate;
* quota blocks are not symbol failures.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from tradehub_research.db import ResearchDB
from tradehub_research.ops import daily_refresh as dr

AS_OF = date(2026, 9, 18)
WINDOW_SESSIONS = 6


def _failed(streak: int = 1, last: str = "2026-09-17T00:00:00+00:00") -> dict:
    return {"streak": streak, "last_attempt_at": last}


def _ledger(tmp_path, rows):
    """A migrated experiment DB holding the given ``backfill_attempt`` rows."""
    from tradehub_research.validation.experiment_db import ExperimentDB

    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    with db.connect() as conn:
        for index, (ticker, status, error, when) in enumerate(rows):
            conn.execute(
                "INSERT INTO backfill_attempt(attempt_id, provider, symbol_or_cik, status, "
                "http_status, bytes, error, requested_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"attempt-{index}", "tiingo", ticker, status, 200, 0, error, when),
            )
    return db


class TestFailureState:
    """Durable state is the ledger, so the rotation and the audit cannot disagree."""

    def test_consecutive_failures_accumulate_into_a_streak(self, tmp_path):
        db = _ledger(
            tmp_path,
            [
                ("AAA", "ERROR", "NETWORK: read timeout", "2026-09-17T00:00:00+00:00"),
                ("AAA", "ERROR", "PROVIDER_ERROR: 500", "2026-09-16T00:00:00+00:00"),
                ("AAA", "SUCCESS", None, "2026-09-15T00:00:00+00:00"),
            ],
        )
        state = dr.attempt_failure_state(db, ["AAA"])
        assert state["AAA"]["streak"] == 2
        assert state["AAA"]["last_attempt_at"] == "2026-09-17T00:00:00+00:00"

    def test_a_landing_fetch_resets_the_streak(self, tmp_path):
        db = _ledger(
            tmp_path,
            [
                ("AAA", "SUCCESS", None, "2026-09-17T00:00:00+00:00"),
                ("AAA", "ERROR", "NETWORK: read timeout", "2026-09-16T00:00:00+00:00"),
            ],
        )
        assert dr.attempt_failure_state(db, ["AAA"])["AAA"]["streak"] == 0

    def test_a_quota_block_is_neither_a_failure_nor_a_reset(self, tmp_path):
        """A spent budget says nothing about the symbol.

        Counting it would turn one quota pause into a fake per-symbol failure
        streak -- and then fairness would hold back symbols that never failed.
        """
        db = _ledger(
            tmp_path,
            [
                ("AAA", "ERROR", "QUOTA: hourly reserve exhausted", "2026-09-17T00:00:00+00:00"),
                ("AAA", "ERROR", "PROVIDER_ERROR: 500", "2026-09-16T00:00:00+00:00"),
            ],
        )
        state = dr.attempt_failure_state(db, ["AAA"])
        assert state["AAA"]["streak"] == 1, "only the provider error is a symbol failure"
        assert state["AAA"]["last_attempt_at"] == "2026-09-17T00:00:00+00:00"

    def test_skipped_quota_status_is_not_a_symbol_failure(self, tmp_path):
        db = _ledger(tmp_path, [("AAA", "SKIPPED_QUOTA", None, "2026-09-17T00:00:00+00:00")])
        assert dr.attempt_failure_state(db, ["AAA"])["AAA"]["streak"] == 0

    def test_a_stale_failure_decays_out_of_the_lookback_window(self, tmp_path):
        """An old failure must not damn a symbol forever."""
        old = (
            datetime.now(timezone.utc) - timedelta(days=dr.FAILURE_LOOKBACK_DAYS + 5)
        ).isoformat()
        db = _ledger(tmp_path, [("AAA", "ERROR", "NETWORK: read timeout", old)])
        assert dr.attempt_failure_state(db, ["AAA"])["AAA"]["streak"] == 0

    def test_untried_symbols_and_a_missing_ledger_are_healthy(self, tmp_path):
        db = _ledger(tmp_path, [])
        assert dr.attempt_failure_state(db, ["AAA"])["AAA"]["streak"] == 0
        assert dr.attempt_failure_state(None, ["AAA"])["AAA"]["streak"] == 0

    def test_only_requested_symbols_are_ranked(self, tmp_path):
        db = _ledger(tmp_path, [("OTHER", "ERROR", "NETWORK: x", "2026-09-17T00:00:00+00:00")])
        state = dr.attempt_failure_state(db, ["AAA"])
        assert set(state) == {"AAA"}


class TestBudgetAllocation:
    """The slice: ready first, cooling bounded, nothing monopolised."""

    def test_a_persistent_failure_cannot_block_a_healthy_candidate(self):
        """Budget 1, and the failing symbol is the oldest.

        Under plain staleness ordering the failure wins the only slot; under the
        fairness slice the healthy candidate gets it.
        """
        to_attempt, deferrals = dr.allocate_rotation(
            ["ZFAIL", "AHEALTHY"], budget=1, failures={"ZFAIL": _failed(2)}
        )
        assert to_attempt == ["AHEALTHY"]
        assert deferrals == {"ZFAIL": dr.refresh_runs.DEFERRED_COOLING}

    def test_a_budget_sized_cohort_of_failures_cannot_starve_the_remainder(self):
        """The review's exact scenario: as many failures as the whole budget."""
        failures = {f"F{i}": _failed(3) for i in range(3)}
        to_attempt, deferrals = dr.allocate_rotation(
            ["F0", "F1", "F2", "H1", "H2", "H3"], budget=3, failures=failures
        )
        assert to_attempt == ["H1", "H2", "H3"], "healthy names must never be crowded out"
        assert set(deferrals) == set(failures)
        assert set(deferrals.values()) == {dr.refresh_runs.DEFERRED_COOLING}

    def test_transient_failures_remain_retryable(self):
        """Once the ready pool leaves room, a cooling symbol is attempted again."""
        to_attempt, _deferrals = dr.allocate_rotation(
            ["RETRYME", "A", "B"], budget=3, failures={"RETRYME": _failed(1)}
        )
        assert "RETRYME" in to_attempt

    def test_a_quota_only_history_keeps_a_symbol_in_the_ready_pool(self):
        """streak 0 == the ledger recorded no *symbol* failure."""
        to_attempt, deferrals = dr.allocate_rotation(
            ["ZOLD", "ANEW"], budget=1, failures={"ZOLD": _failed(0)}
        )
        assert to_attempt == ["ZOLD"], "staleness still wins when nothing failed"
        assert deferrals == {"ANEW": dr.refresh_runs.DEFERRED_BUDGET}

    def test_the_cooling_share_is_bounded_so_failures_cannot_monopolise(self):
        failures = {f"F{i}": _failed(4) for i in range(20)}
        budget = 20
        to_attempt, deferrals = dr.allocate_rotation(
            [f"F{i}" for i in range(20)], budget=budget, failures=failures
        )
        assert len(to_attempt) == max(1, budget // dr.COOLING_BUDGET_DIVISOR)
        assert len(deferrals) == 20 - len(to_attempt)
        assert set(deferrals.values()) == {dr.refresh_runs.DEFERRED_COOLING}

    def test_cooling_retries_rotate_least_recently_attempted_first(self):
        """Otherwise the slice itself would starve the cooling tail."""
        failures = {
            "F0": _failed(2, "2026-09-18T00:00:00+00:00"),
            "F1": _failed(2, "2026-09-10T00:00:00+00:00"),
        }
        to_attempt, _deferrals = dr.allocate_rotation(["F0", "F1"], budget=1, failures=failures)
        assert to_attempt == ["F1"]

    def test_successful_candidates_keep_their_staleness_priority(self):
        """Fairness must not disturb the ordering of healthy candidates."""
        to_attempt, _deferrals = dr.allocate_rotation(["ZWORST", "ABETTER"], budget=1)
        assert to_attempt == ["ZWORST"]

    def test_a_zero_budget_attempts_nothing_and_defers_everything(self):
        to_attempt, deferrals = dr.allocate_rotation(["A", "B"], budget=0)
        assert to_attempt == []
        assert set(deferrals.values()) == {dr.refresh_runs.DEFERRED_BUDGET}


class TestPersistentFailureAcrossRuns:
    """End-to-end through ``run_daily_refresh``: the healthy name goes first,
    the failing one is still reached afterwards."""

    def _harness(self, tmp_path, monkeypatch, bars, failures):
        research_db = ResearchDB(tmp_path / "research.db", 5000)
        research_db.migrate()
        from tradehub_research.validation.experiment_db import ExperimentDB

        experiment_db = ExperimentDB(tmp_path / "experiment.db")
        experiment_db.migrate()

        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: set())
        monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, _t: failures)

        attempted: list[str] = []

        def fake_refresh_one(_adapter, _quota, _rdb, _edb, _store, ticker, as_of, _summary):
            attempted.append(ticker)
            _summary["SUCCESS"] += 1
            if not ticker.startswith("ZFAIL"):
                bars[f"S-{ticker}"] = as_of.isoformat()  # the healthy one heals

        monkeypatch.setattr(dr, "_refresh_one", fake_refresh_one)

        class _Quota:
            def bootstrap_usage(self, _now, _limit):
                return {"used": 0, "symbols": []}

        monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))
        return (
            SimpleNamespace(
                busy_timeout_ms=5000,
                tiingo_token=None,
                tiingo_license_confirmed=True,
                adapter_cache_dir=tmp_path,
            ),
            SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path),
            experiment_db,
            attempted,
        )

    def test_a_persistent_failure_is_deferred_then_retried_after_the_healthy_name(
        self, tmp_path, monkeypatch
    ):
        bars = {"S-ZFAIL": "2026-08-01", "S-AHEALTHY": "2026-08-01"}
        failures = {"ZFAIL": _failed(2)}
        settings, paths, experiment_db, attempted = self._harness(
            tmp_path, monkeypatch, bars, failures
        )

        first = dr.run_daily_refresh(
            settings=settings,
            experiment_db=experiment_db,
            paths=paths,
            as_of=AS_OF,
            rotation_budget=1,
        )
        assert attempted == ["AHEALTHY"], "the failing symbol must not take the only slot"
        assert first["rotation_cooling"] == 1
        assert first["refresh_run_deferred"] == 1

        # Next run: the healthy name now holds the expected session, so the run's
        # only candidate is the failing one -- which is therefore still attempted.
        second = dr.run_daily_refresh(
            settings=settings,
            experiment_db=experiment_db,
            paths=paths,
            as_of=AS_OF,
            rotation_budget=1,
        )
        assert attempted == ["AHEALTHY", "ZFAIL"], "the failure must remain retryable"
        assert second["rotation_refreshed"] == 1
        assert second["rotation_cooling"] == 1


class TestRefreshRunRecord:
    """The fairness decision is durable, not stdout."""

    def test_a_completed_run_records_served_failed_and_deferred(self, tmp_path, monkeypatch):
        bars = {"S-ZFAIL": "2026-08-01", "S-AHEALTHY": "2026-08-01"}
        harness = TestPersistentFailureAcrossRuns()
        settings, paths, experiment_db, _attempted = harness._harness(
            tmp_path, monkeypatch, bars, {"ZFAIL": _failed(2)}
        )
        summary = dr.run_daily_refresh(
            settings=settings,
            experiment_db=experiment_db,
            paths=paths,
            as_of=AS_OF,
            rotation_budget=1,
        )
        assert summary["refresh_run_status"] == dr.refresh_runs.COMPLETED

        store = dr.refresh_runs.store_for(paths)
        assert store.outcomes(AS_OF.isoformat()) == {
            "AHEALTHY": dr.refresh_runs.REFRESHED,
            "ZFAIL": dr.refresh_runs.DEFERRED_COOLING,
        }
        assert store.completed_deferrals(AS_OF.isoformat()) == {
            "ZFAIL": dr.refresh_runs.DEFERRED_COOLING
        }
        record = store.run(AS_OF.isoformat())
        assert record["rotation_budget"] == 1
        assert record["candidates"] == 2

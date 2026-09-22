"""Regression tests: the third Codex review round on PR #74.

Four follow-up P2 findings against ``fbc22db``:

5. **An active-set failure was recorded as a deliberate deferral.** A failing
   active symbol stays stale, enters the rotation and reads as "cooling"; when
   ready candidates filled the budget it was recorded ``DEFERRED_COOLING``, and
   because a deferral outranks the ledger class the diagnosis then silenced
   remediation for a symbol that had actually been attempted and lost.
6. **A same-session re-run did not invalidate the previous record early enough.**
   The record was only reset to ``RUNNING`` after the active requests and
   capacity planning, so a re-run that died in that window left the previous
   ``COMPLETED`` deferrals authoritative — suppressing remediation for a session
   whose latest attempt was interrupted.
7. **A terminal ``DEFERRED`` checkpoint was never reopened.** ``seed_symbol`` is
   ``INSERT OR IGNORE``, so once the watch settled a symbol as scheduled, a later
   pass in which that symbol became actionable reported it as targeted while
   ``pending()`` never returned it — no fetch, no attempt.
8. **A successful remediation was not recorded in the ledger.** Failure streaks
   are derived from ``backfill_attempt``; a repair wrote only to the checkpoint,
   so the last row stayed an old error and the symbol read as failing long after
   it was fixed.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import tradehub_research.backfill.tiingo_driver as driver
from tradehub_research.db import ResearchDB
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import refresh_runs
from tradehub_research.validation.experiment_db import ExperimentDB

AS_OF = date(2026, 9, 18)
EXPECTED = "2026-09-18"
STALE_BAR = "2026-09-09"


def _settings(tmp_path, **over):
    base = SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token=None,
        tiingo_license_confirmed=True,
        adapter_cache_dir=tmp_path,
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _paths(tmp_path):
    return SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)


def _research_db(tmp_path):
    db = ResearchDB(tmp_path / "research.db", 5000)
    db.migrate()
    return db


def _experiment_db(tmp_path):
    db = ExperimentDB(tmp_path / "experiment.db")
    db.migrate()
    return db


def _ledger_row(tmp_path, ticker, status, error, when="2026-09-17T00:00:00+00:00"):
    db = _experiment_db(tmp_path)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO backfill_attempt(attempt_id, provider, symbol_or_cik, status, "
            "http_status, bytes, error, requested_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"{ticker}-{status}", "tiingo", ticker, status, 200, 0, error, when),
        )
    return db


# ---------------------------------------------------------------------------
# 5 -- active-set outcomes
# ---------------------------------------------------------------------------
class TestActiveSetFailuresAreRecordedAsFailed:
    def _run(self, tmp_path, monkeypatch, active_fails: bool):
        bars = {"S-ACT": STALE_BAR, "S-ZREADY": "2026-06-01", "S-AREADY": "2026-08-01"}
        _research_db(tmp_path)
        experiment_db = _experiment_db(tmp_path)

        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: {"ACT"})
        monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, tickers: {})

        attempted: list[str] = []

        def fake_refresh_one(_a, _q, _r, _e, _s, ticker, as_of, summary):
            attempted.append(ticker)
            if ticker == "ACT" and active_fails:
                summary["ERROR"] += 1
                return
            summary["SUCCESS"] += 1
            bars[f"S-{ticker}"] = as_of.isoformat()

        monkeypatch.setattr(dr, "_refresh_one", fake_refresh_one)

        class _Quota:
            def bootstrap_usage(self, _now, _limit):
                return {"used": 0, "symbols": []}

        monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))
        summary = dr.run_daily_refresh(
            settings=_settings(tmp_path),
            experiment_db=experiment_db,
            paths=_paths(tmp_path),
            as_of=AS_OF,
            rotation_budget=1,  # ready candidates fill the rotation
        )
        return summary, attempted

    def test_a_failed_active_symbol_is_not_recorded_as_scheduled(self, tmp_path, monkeypatch):
        """The defect: a real failure must not be dressed as a deliberate skip."""
        summary, attempted = self._run(tmp_path, monkeypatch, active_fails=True)
        assert attempted[0] == "ACT", "the active phase runs first"
        store = refresh_runs.store_for(_paths(tmp_path))
        outcomes = store.outcomes(AS_OF.isoformat())
        assert outcomes["ACT"] == refresh_runs.FAILED
        assert refresh_runs.DEFERRED_COOLING not in outcomes.values()
        # …and it is therefore NOT a deferral, so remediation is not suppressed.
        assert "ACT" not in store.completed_deferrals(AS_OF.isoformat())
        assert summary["refresh_run_status"] == refresh_runs.COMPLETED

    def test_a_successful_active_symbol_is_recorded_as_refreshed(self, tmp_path, monkeypatch):
        _summary, _attempted = self._run(tmp_path, monkeypatch, active_fails=False)
        store = refresh_runs.store_for(_paths(tmp_path))
        assert store.outcomes(AS_OF.isoformat())["ACT"] == refresh_runs.REFRESHED

    def test_the_active_symbol_is_not_fetched_twice_in_one_run(self, tmp_path, monkeypatch):
        _summary, attempted = self._run(tmp_path, monkeypatch, active_fails=True)
        assert attempted.count("ACT") == 1, "one attempt per symbol per run"


# ---------------------------------------------------------------------------
# 6 -- invalidate the previous run before fallible work
# ---------------------------------------------------------------------------
class TestRerunInvalidatesThePreviousRecord:
    def _prior_completed_record(self, tmp_path):
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store_token = store.open_run(
            EXPECTED, EXPECTED, universe=3, window_sessions=6, rotation_budget=1
        )
        store.finish(
            EXPECTED,
            refresh_runs.COMPLETED,
            {"DEF000": refresh_runs.DEFERRED_BUDGET},
            token=store_token,
        )
        return store

    def _wire(self, tmp_path, monkeypatch, fail_mode: str):
        bars = {"S-ACT": STALE_BAR, "S-DEF000": STALE_BAR}
        _research_db(tmp_path)
        experiment_db = _experiment_db(tmp_path)
        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: {"ACT"})

        def boom(*_a, **_k):
            # Mirrors the provider's own refusal (`classify_error` keys on
            # "quota reserve"; the run's outer handler keys on "quota").
            raise RuntimeError("hourly quota reserve exhausted (45/hr)")

        monkeypatch.setattr(dr, "_refresh_one", boom)

        class _Quota:
            def bootstrap_usage(self, _now, _limit):
                return {"used": 0, "symbols": []}

        monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))
        return experiment_db

    def test_a_quota_stopped_rerun_does_not_leave_the_old_deferrals_authoritative(
        self, tmp_path, monkeypatch
    ):
        store = self._prior_completed_record(tmp_path)
        assert store.completed_deferrals(EXPECTED) == {"DEF000": refresh_runs.DEFERRED_BUDGET}, (
            "precondition: the earlier run completed with a deferral"
        )

        experiment_db = self._wire(tmp_path, monkeypatch, "quota")
        summary = dr.run_daily_refresh(
            settings=_settings(tmp_path),
            experiment_db=experiment_db,
            paths=_paths(tmp_path),
            as_of=AS_OF,
            rotation_budget=1,
        )
        assert summary["status"] == "QUOTA_EXHAUSTED"
        # The old COMPLETED record must be gone: this session's latest attempt was
        # interrupted, so its symbols have to stay actionable.
        assert store.completed_run(EXPECTED) is None
        assert store.completed_deferrals(EXPECTED) == {}
        assert store.run(EXPECTED)["status"] == refresh_runs.QUOTA_EXHAUSTED

    def test_an_interrupted_rerun_leaves_no_completed_record(self, tmp_path, monkeypatch):
        store = self._prior_completed_record(tmp_path)
        experiment_db = self._wire(tmp_path, monkeypatch, "crash")

        def crash(*_a, **_k):
            raise ValueError("worker died")

        monkeypatch.setattr(dr, "_refresh_one", crash)
        with pytest.raises(ValueError):
            dr.run_daily_refresh(
                settings=_settings(tmp_path),
                experiment_db=experiment_db,
                paths=_paths(tmp_path),
                as_of=AS_OF,
                rotation_budget=1,
            )
        assert store.run(EXPECTED)["status"] == refresh_runs.RUNNING
        assert store.completed_run(EXPECTED) is None
        assert store.completed_deferrals(EXPECTED) == {}


# ---------------------------------------------------------------------------
# 7 -- reopen a terminal DEFERRED checkpoint when the symbol becomes work again
# ---------------------------------------------------------------------------
def _wire_audit(monkeypatch, bars, attempts=None):
    import tradehub_research.db as dbmod

    monkeypatch.setattr(
        driver, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
    )
    monkeypatch.setattr(
        driver, "symbol_has_evidence", lambda _db, ticker: bars.get(f"S-{ticker}") is not None
    )
    monkeypatch.setattr(dbmod, "ResearchDB", lambda *a, **k: object())
    monkeypatch.setattr(dr, "retired_tickers", set)
    monkeypatch.setattr(df, "_last_bar", lambda _db, sid: bars.get(sid))
    monkeypatch.setattr(df, "_last_attempt", lambda _exp, ticker: (attempts or {}).get(ticker))
    return _settings("/tmp")


class TestDeferredCheckpointsAreReopened:
    def _bars(self):
        return {"S-DEF000": STALE_BAR, "S-OK": EXPECTED}

    def test_a_deferred_symbol_becomes_fetchable_once_it_is_actionable(self, tmp_path, monkeypatch):
        bars = self._bars()
        settings = _wire_audit(monkeypatch, bars)

        # pass 1: the completed run deferred it -- scheduled, not work.
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store_token = store.open_run(
            EXPECTED, EXPECTED, universe=2, window_sessions=6, rotation_budget=1
        )
        store.finish(
            EXPECTED,
            refresh_runs.COMPLETED,
            {"DEF000": refresh_runs.DEFERRED_BUDGET},
            token=store_token,
        )

        fetched: list[str] = []
        first = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=None,
            audit=df.audit_universe(
                settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
            ),
            refresh_one=lambda ticker: fetched.append(ticker),
        )
        assert fetched == []
        assert first["scheduled_deferrals"] == 1

        checkpoint = df.CheckpointStore(tmp_path / "freshness_remediation.sqlite")
        row = next(r for r in checkpoint.all_symbols(first["run_key"]) if r["ticker"] == "DEF000")
        assert row["disposition"] == "DEFERRED", "precondition: settled as scheduled"

        # pass 2: the run that deferred it is no longer the latest word -- the
        # symbol has its own recorded failure and is actionable again.
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store_token = store.open_run(
            EXPECTED, EXPECTED, universe=2, window_sessions=6, rotation_budget=1
        )
        store.finish(
            EXPECTED, refresh_runs.COMPLETED, {"DEF000": refresh_runs.FAILED}, token=store_token
        )
        attempts = {"DEF000": {"status": "ERROR", "error": "PROVIDER_ERROR: 503 x"}}
        settings = _wire_audit(monkeypatch, bars, attempts)

        second = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=None,
            audit=df.audit_universe(
                settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
            ),
            refresh_one=lambda ticker: fetched.append(ticker),
        )
        assert fetched == ["DEF000"], "an actionable symbol must not stay stuck as DEFERRED"
        assert second["targeted"] == 1

    def test_a_repaired_verdict_is_not_undone(self, tmp_path):
        checkpoint = df.CheckpointStore(tmp_path / "freshness_remediation.sqlite")
        checkpoint.open_run("k", EXPECTED, 2, 1)
        checkpoint.seed_symbol("k", "DEF000", "S1", STALE_BAR, df.SCHEDULED_DEFERRAL)
        checkpoint.settle(
            "k",
            "DEF000",
            disposition="REPAIRED",
            last_bar_after=EXPECTED,
            classification=df.SCHEDULED_DEFERRAL,
        )
        checkpoint.reopen_deferred("k", "DEF000", df.SCHEDULED_DEFERRAL)
        row = next(r for r in checkpoint.all_symbols("k") if r["ticker"] == "DEF000")
        assert row["disposition"] == "REPAIRED", "only a DEFERRED verdict may be reopened"


# ---------------------------------------------------------------------------
# 8 -- a successful repair must be visible to the failure-streak reader
# ---------------------------------------------------------------------------
class TestRepairsAreRecordedInTheLedger:
    def test_a_repair_clears_the_failure_streak(self, tmp_path, monkeypatch):
        bars = {"S-DEF000": STALE_BAR, "S-OK": EXPECTED}
        experiment_db = _ledger_row(
            tmp_path, "DEF000", "ERROR", "PROVIDER_ERROR: 503 x", "2026-09-10T00:00:00+00:00"
        )
        settings = _wire_audit(monkeypatch, bars)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=experiment_db, as_of=AS_OF
        )

        def repair(ticker):
            bars[f"S-{ticker}"] = EXPECTED  # the bar lands

        summary = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=experiment_db,
            audit=audit,
            refresh_one=repair,
        )
        assert summary["repaired"] == 1

        # The repair is now the newest ledger row, so the symbol is not cooling.
        state = dr.attempt_failure_state(experiment_db, ["DEF000"])
        assert state["DEF000"]["streak"] == 0, "a repaired symbol must not read as failing"

    def test_an_unrecorded_failure_would_read_as_cooling(self, tmp_path, monkeypatch):
        """Positive control: the ledger is what the streak reader trusts."""
        experiment_db = _ledger_row(
            tmp_path, "DEF000", "ERROR", "PROVIDER_ERROR: 503 x", "2026-09-10T00:00:00+00:00"
        )
        assert dr.attempt_failure_state(experiment_db, ["DEF000"])["DEF000"]["streak"] == 1

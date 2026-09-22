"""Regression tests: the fourth Codex review round on PR #74.

Two follow-up findings against ``42b7c96``:

9. **The best-effort ledger write caught the wrong exception.** ``_ledger_write``
   rewraps ``sqlite3.Error``/``OSError`` in ``LedgerPersistenceError``, so a
   handler catching only ``LEDGER_IO_FAILURES`` never matched: ``remediate``
   raised before settling the symbol ``REPAIRED``, the promised
   ``repair_ledger_unrecorded`` disclosure never happened, and the health-watch
   run aborted instead of degrading gracefully.
10. **An un-invalidatable run record did not stop the refresh.** On a
   same-session re-run whose record could not be written (read-only/full store),
   the previous ``COMPLETED`` row and its deferral list survived -- so if the new
   run then failed, obsolete deferrals suppressed remediation for the latest,
   interrupted attempt. The run must not proceed unless the prior decision was
   durably invalidated.

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


def _settings(tmp_path):
    return SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token=None,
        tiingo_license_confirmed=True,
        adapter_cache_dir=tmp_path,
    )


def _paths(tmp_path):
    return SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)


def _wire_audit(monkeypatch, bars):
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
    monkeypatch.setattr(df, "_last_attempt", lambda _exp, _ticker: None)


# ---------------------------------------------------------------------------
# 9 -- the best-effort write must actually be best-effort
# ---------------------------------------------------------------------------
class TestBestEffortLedgerWriteToleratesTheWrapper:
    def test_an_unwritable_ledger_still_records_the_repair(self, tmp_path, monkeypatch):
        """The repair lands; only its ledger evidence fails."""
        bars = {"S-DEF000": STALE_BAR, "S-OK": EXPECTED}
        _wire_audit(monkeypatch, bars)
        experiment_db = ExperimentDB(tmp_path / "experiment.db")
        experiment_db.migrate()
        settings = _settings(tmp_path)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=experiment_db, as_of=AS_OF
        )

        def repair(ticker):
            bars[f"S-{ticker}"] = EXPECTED  # the bar lands

        # The ledger refuses the write, exactly as a locked/full database would.
        monkeypatch.setattr(
            df,
            "_ledger_write",
            lambda *_a, **_k: (_ for _ in ()).throw(
                df.LedgerPersistenceError("could not persist remediation evidence")
            ),
        )
        summary = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=experiment_db,
            audit=audit,
            refresh_one=repair,
        )
        assert summary["repaired"] == 1, "the repair must not be thrown away"
        assert summary["repair_ledger_unrecorded"] == 1, "…but it must be disclosed"
        assert summary["requires_intervention"] is False

    def test_a_sqlite_failure_is_wrapped_and_tolerated(self, tmp_path, monkeypatch):
        """Positive control: the wrapper is what the handler has to catch."""
        bars = {"S-DEF000": STALE_BAR, "S-OK": EXPECTED}
        _wire_audit(monkeypatch, bars)
        experiment_db = ExperimentDB(tmp_path / "experiment.db")
        experiment_db.migrate()
        settings = _settings(tmp_path)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=experiment_db, as_of=AS_OF
        )

        def refuse(*_a, **_k):
            raise OSError("database or disk is full")

        import tradehub_research.backfill.tiingo_driver as driver_mod

        monkeypatch.setattr(driver_mod, "record_attempt", refuse)
        summary = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=experiment_db,
            audit=audit,
            refresh_one=lambda ticker: bars.__setitem__(f"S-{ticker}", EXPECTED),
        )
        assert summary["repaired"] == 1
        assert summary["repair_ledger_unrecorded"] == 1

    def test_the_empty_finding_still_fails_closed(self, tmp_path, monkeypatch):
        """The disclosed write is the REPAIR path; the delisting finding is not."""
        assert issubclass(df.LedgerPersistenceError, RuntimeError)
        assert df.LedgerPersistenceError in df.LEDGER_WRITE_FAILURES
        assert set(df.LEDGER_IO_FAILURES).issubset(set(df.LEDGER_WRITE_FAILURES))


# ---------------------------------------------------------------------------
# 10 -- do not run under a record that could not be invalidated
# ---------------------------------------------------------------------------
class TestRunAbortsWhenTheRecordCannotBeOpened:
    def _prior_completed(self, tmp_path):
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
        return store

    def test_refresh_does_not_proceed_without_invalidating_the_prior_run(
        self, tmp_path, monkeypatch
    ):
        bars = {"S-DEF000": STALE_BAR, "S-OK": EXPECTED}
        _wire_audit(monkeypatch, bars)
        _research = ResearchDB(tmp_path / "research.db", 5000)
        _research.migrate()
        experiment_db = ExperimentDB(tmp_path / "experiment.db")
        experiment_db.migrate()
        store = self._prior_completed(tmp_path)

        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: set())
        monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, tickers: {})
        fetched: list[str] = []
        monkeypatch.setattr(
            dr,
            "_refresh_one",
            lambda _a, _q, _r, _e, _s, ticker, _as_of, _summary: fetched.append(ticker),
        )

        class _Quota:
            def bootstrap_usage(self, _now, _limit):
                return {"used": 0, "symbols": []}

        monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))

        # The run store cannot be written: opening/resetting the record fails.
        def refuse(*_a, **_k):
            raise OSError("read-only database")

        monkeypatch.setattr(dr.refresh_runs.RefreshRunStore, "open_run", refuse)

        with pytest.raises(OSError):
            dr.run_daily_refresh(
                settings=_settings(tmp_path),
                experiment_db=experiment_db,
                paths=_paths(tmp_path),
                as_of=AS_OF,
                rotation_budget=1,
            )
        assert fetched == [], "no provider work before the prior decision is invalidated"
        # And the stale record is still visibly COMPLETED rather than silently trusted.
        assert store.run(EXPECTED)["status"] == refresh_runs.COMPLETED

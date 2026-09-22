"""Regression tests: the second Codex review round on PR #74.

Two follow-up P2 findings against the first remediation round:

3. **A cooling deferral lost to the ledger-derived class.** ``DEFERRED_COOLING``
   means the symbol was withheld *because* its recent attempts failed, so a
   recent failing ledger row always exists. Classifying from that row first made
   every deliberately cooled symbol a ``TRANSIENT_PROVIDER_ERROR`` again, so the
   watch handed it back to remediation -- up to three immediate retries,
   bypassing the fairness slice and spending the quota the slice protects. The
   completed run's disposition must outrank the ledger class, while a symbol the
   run actually tried and *lost* (``FAILED``) keeps its failure classification.

4. **A capacity-rejected candidate held a budget slot.** Allocation ran over all
   candidates, so a symbol the rolling-month ceiling refused could take a slot
   that the attempt loop then skipped -- the run spent fewer requests than its
   budget allowed and left later admitted names marked deferred.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import refresh_runs

EXPECTED = "2026-09-18"
AS_OF = date(2026, 9, 18)
STALE = [f"S-DEF{i:03d}" for i in range(3)]
RECENT_FAILURE = {"status": "ERROR", "http_status": 503, "error": "PROVIDER_ERROR: 503 x"}


def _wire(monkeypatch, bars, attempts=None):
    import tradehub_research.backfill.tiingo_driver as driver
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
    monkeypatch.setattr(
        df,
        "_last_attempt",
        lambda _exp, ticker: (attempts or {}).get(ticker),
    )
    return SimpleNamespace(busy_timeout_ms=5000, tiingo_token=None, adapter_cache_dir="/tmp")


def _bars():
    bars = {sid: "2026-09-09" for sid in STALE}
    bars["S-OK000"] = EXPECTED  # keeps the audit otherwise quiet
    return bars


def _paths(tmp_path):
    return SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)


def _record(tmp_path, outcomes: dict[str, str]) -> None:
    store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
    store_token = store.open_run(
        EXPECTED, EXPECTED, universe=4, window_sessions=6, rotation_budget=1
    )
    store.finish(EXPECTED, refresh_runs.COMPLETED, outcomes, token=store_token)


def _audit(monkeypatch, tmp_path):
    bars = _bars()
    attempts = {ticker[2:]: dict(RECENT_FAILURE) for ticker in STALE}
    settings = _wire(monkeypatch, bars, attempts)
    audit = df.audit_universe(
        settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
    )
    return bars, settings, audit


class TestCoolingDeferralOutranksTheLedgerClass:
    def test_a_deliberately_cooled_symbol_is_scheduled_not_a_provider_error(
        self, monkeypatch, tmp_path
    ):
        """The exact ordering bug: the ledger row would otherwise win."""
        bars, settings, _ = _audit(monkeypatch, tmp_path)
        _record(
            tmp_path,
            {
                "DEF000": refresh_runs.DEFERRED_COOLING,
                "DEF001": refresh_runs.DEFERRED_BUDGET,
                "DEF002": refresh_runs.FAILED,  # attempted and lost
            },
        )
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        by_ticker = {row.ticker: row.classification for row in audit.stale}
        assert by_ticker["DEF000"] == df.SCHEDULED_DEFERRAL
        assert by_ticker["DEF001"] == df.SCHEDULED_DEFERRAL
        # A symbol the run tried and lost is NOT a deferral: it keeps its failure
        # class so it stays remediable work.
        assert by_ticker["DEF002"] == df.PROVIDER_TRANSIENT
        assert audit.scheduled_count == 2
        assert set(bars) == {"S-DEF000", "S-DEF001", "S-DEF002", "S-OK000"}

    def test_remediation_does_not_retry_the_cooled_symbol(self, monkeypatch, tmp_path):
        bars, settings, _ = _audit(monkeypatch, tmp_path)
        _record(
            tmp_path,
            {
                "DEF000": refresh_runs.DEFERRED_COOLING,
                "DEF001": refresh_runs.DEFERRED_BUDGET,
                "DEF002": refresh_runs.FAILED,
            },
        )
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        fetched: list[str] = []
        summary = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=None,
            audit=audit,
            refresh_one=lambda ticker: fetched.append(ticker),
        )
        assert fetched == ["DEF002"], "only the symbol that was attempted and failed is work"
        assert summary["scheduled_deferrals"] == 2
        assert summary["targeted"] == 1
        assert set(bars) == {"S-DEF000", "S-DEF001", "S-DEF002", "S-OK000"}

    def test_without_a_deferral_record_the_ledger_class_still_applies(self, monkeypatch, tmp_path):
        """Positive control: the reordering must not swallow real provider errors."""
        _bars_unused, settings, audit = _audit(monkeypatch, tmp_path)
        assert {row.classification for row in audit.stale} == {df.PROVIDER_TRANSIENT}
        assert audit.scheduled_count == 0


class TestCapacityRejectedCandidatesDoNotHoldSlots:
    """Allocation must run over the admitted set, not the raw candidate list."""

    def _harness(self, tmp_path, monkeypatch, candidates, admitted):
        research_db = ResearchDB(tmp_path / "research.db", 5000)
        research_db.migrate()
        from tradehub_research.validation.experiment_db import ExperimentDB

        experiment_db = ExperimentDB(tmp_path / "experiment.db")
        experiment_db.migrate()

        bars = {f"S-{ticker}": "2026-08-01" for ticker in candidates}
        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: set())
        monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, tickers: {})

        import tradehub_research.ops.symbol_capacity as capacity

        class _Plan:
            already_reserved = list(admitted)
            admissible_new: list[str] = []
            deferred: list[str] = []

            def as_dict(self):
                return {}

        # `run_daily_refresh` imports this inside the function, so patch it where
        # it is looked up.
        monkeypatch.setattr(capacity, "plan_symbol_capacity", lambda *a, **k: _Plan())

        attempted: list[str] = []

        def fake_refresh_one(_a, _q, _r, _e, _s, ticker, as_of, summary):
            attempted.append(ticker)
            summary["SUCCESS"] += 1
            bars[f"S-{ticker}"] = as_of.isoformat()

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

    def test_the_full_budget_is_spent_on_admitted_candidates(self, tmp_path, monkeypatch):
        """A refused candidate must not consume a request the run may spend.

        ZNEW is the oldest but the ceiling refuses it; AOK1/AOK2 are admitted.
        Budget 2 must therefore fetch both admitted names, not skip ZNEW and
        leave AOK2 waiting.
        """
        candidates = ["ZNEW", "AOK1", "AOK2"]
        settings, paths, experiment_db, attempted = self._harness(
            tmp_path, monkeypatch, candidates, admitted=["AOK1", "AOK2"]
        )
        summary = dr.run_daily_refresh(
            settings=settings,
            experiment_db=experiment_db,
            paths=paths,
            as_of=AS_OF,
            rotation_budget=2,
        )
        assert attempted == ["AOK1", "AOK2"]
        assert summary["rotation_refreshed"] == 2
        assert summary["rotation_candidates"] == 3
        assert summary["rotation_deferred_to_next_run"] == 0  # both admitted names served

        store = refresh_runs.store_for(paths)
        assert store.outcomes(AS_OF.isoformat()) == {
            "ZNEW": refresh_runs.DEFERRED_CAPACITY,
            "AOK1": refresh_runs.REFRESHED,
            "AOK2": refresh_runs.REFRESHED,
        }
        assert store.completed_deferrals(AS_OF.isoformat()) == {
            "ZNEW": refresh_runs.DEFERRED_CAPACITY
        }

    def test_a_refused_candidate_that_is_not_admitted_is_disclosed(self, tmp_path, monkeypatch):
        candidates = ["ZNEW", "AOK1"]
        settings, paths, experiment_db, attempted = self._harness(
            tmp_path, monkeypatch, candidates, admitted=["AOK1"]
        )
        summary = dr.run_daily_refresh(
            settings=settings,
            experiment_db=experiment_db,
            paths=paths,
            as_of=AS_OF,
            rotation_budget=1,
        )
        assert attempted == ["AOK1"]
        assert summary["rotation_refreshed"] == 1
        assert summary["rotation_deferred_to_next_run"] == 0


@pytest.mark.parametrize("reason", sorted(refresh_runs.DEFERRED))
def test_every_deferral_reason_is_treated_as_scheduled(monkeypatch, tmp_path, reason):
    """Any deliberate deferral reason suppresses remediation, not just the budget one."""
    bars = _bars()
    settings = _wire(monkeypatch, bars, {t[2:]: dict(RECENT_FAILURE) for t in STALE})
    _record(
        tmp_path, {"DEF000": reason, "DEF001": refresh_runs.FAILED, "DEF002": refresh_runs.FAILED}
    )
    audit = df.audit_universe(
        settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
    )
    by_ticker = {row.ticker: row.classification for row in audit.stale}
    assert by_ticker["DEF000"] == df.SCHEDULED_DEFERRAL
    assert audit.scheduled_count == 1

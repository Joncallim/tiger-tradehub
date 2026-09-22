"""Regression tests: the seventh Codex review round on PR #74.

Two follow-up P2 findings against ``c6bee0e``:

A. The cooling reservation was not capped by the retries that exist. Reserving the
   full allowance when only one symbol is cooling idled the rest: budget 74 with a
   single cooling symbol and ≥74 ready candidates selected 56 ready + 1 cooling,
   wasting 17 requests while stale names were deferred.
B. A completed run's deferral was ignored for a candidate with evidence but no
   usable bar (``last is None``): the earlier ``CHECKPOINT_LOST`` branch short-
   circuited before the deferral check, so remediation fetched it immediately --
   bypassing the bounded schedule, and for a ``CAPACITY_DEFERRED`` symbol the
   rolling-month ceiling that had refused it.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import tradehub_research.backfill.tiingo_driver as driver
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import refresh_runs

AS_OF = date(2026, 9, 18)
EXPECTED = "2026-09-18"


def _failed(streak: int = 1) -> dict:
    return {"streak": streak, "last_attempt_at": "2026-09-17T00:00:00+00:00"}


# ---------------------------------------------------------------------------
# A -- the reservation is capped by available retries
# ---------------------------------------------------------------------------
class TestTheReservationIsCappedByAvailableRetries:
    def test_a_single_cooling_symbol_does_not_idle_the_budget(self):
        """The review's exact case: budget 74, one cooling symbol."""
        ready = [f"R{i:02d}" for i in range(80)]
        to_attempt, deferrals = dr.allocate_rotation(
            [*ready, "ZCOOL"], budget=74, failures={"ZCOOL": _failed()}
        )
        assert len(to_attempt) == 74, "the budget must be spent in full"
        assert "ZCOOL" in to_attempt
        assert len([t for t in to_attempt if t.startswith("R")]) == 73
        assert len(deferrals) == len(ready) - 73

    def test_two_cooling_symbols_reserve_exactly_two(self):
        ready = [f"R{i:02d}" for i in range(30)]
        to_attempt, _deferrals = dr.allocate_rotation(
            [*ready, "Z1", "Y1"], budget=10, failures={"Z1": _failed(), "Y1": _failed()}
        )
        assert len(to_attempt) == 10
        assert len([t for t in to_attempt if t.startswith(("Z", "Y"))]) == 2

    def test_a_large_cooling_pool_still_reserves_only_its_allowance(self):
        ready = [f"R{i:02d}" for i in range(30)]
        cooling = {f"C{i:02d}": _failed(3) for i in range(30)}
        to_attempt, _deferrals = dr.allocate_rotation(
            [*ready, *cooling], budget=40, failures=cooling
        )
        assert len(to_attempt) == 40
        assert len([t for t in to_attempt if t.startswith("C")]) == 40 // dr.COOLING_BUDGET_DIVISOR


# ---------------------------------------------------------------------------
# B -- a deferral applies to no-bar candidates too
# ---------------------------------------------------------------------------
def _wire(monkeypatch, bars, evidence):
    import tradehub_research.db as dbmod

    monkeypatch.setattr(
        driver, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
    )
    monkeypatch.setattr(driver, "symbol_has_evidence", lambda _db, ticker: evidence[ticker])
    monkeypatch.setattr(dbmod, "ResearchDB", lambda *a, **k: object())
    monkeypatch.setattr(dr, "retired_tickers", set)
    monkeypatch.setattr(df, "_last_bar", lambda _db, sid: bars.get(sid))
    monkeypatch.setattr(df, "_last_attempt", lambda _exp, _ticker: None)
    return SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token=None,
        tiingo_license_confirmed=True,
        adapter_cache_dir="/tmp",
    )


def _audit(monkeypatch, tmp_path, bars, evidence, deferrals):
    if deferrals:
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store.open_run(EXPECTED, EXPECTED, universe=len(bars), window_sessions=6, rotation_budget=1)
        store.finish(EXPECTED, refresh_runs.COMPLETED, deferrals)
    settings = _wire(monkeypatch, bars, evidence)
    paths = SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)
    return (
        df.audit_universe(settings=settings, paths=paths, experiment_db=None, as_of=AS_OF),
        settings,
        paths,
    )


class TestDeferralAppliesToNoBarCandidates:
    def test_a_capacity_deferred_symbol_without_bars_is_not_fetched(self, tmp_path, monkeypatch):
        """The dangerous case: re-fetching would bypass the rolling-month ceiling."""
        bars = {"S-CAP": None}
        evidence = {"CAP": True}
        audit, _settings_, _paths_ = _audit(
            monkeypatch, tmp_path, bars, evidence, {"CAP": refresh_runs.DEFERRED_CAPACITY}
        )
        row = next(r for r in audit.stale if r.ticker == "CAP")
        assert row.classification == df.SCHEDULED_DEFERRAL
        assert audit.scheduled_count == 1

    def test_a_budget_deferred_symbol_without_bars_is_scheduled_not_lost(
        self, tmp_path, monkeypatch
    ):
        bars = {"S-CAP": None}
        audit, _s, _p = _audit(
            monkeypatch, tmp_path, bars, {"CAP": True}, {"CAP": refresh_runs.DEFERRED_BUDGET}
        )
        row = next(r for r in audit.stale if r.ticker == "CAP")
        assert row.classification == df.SCHEDULED_DEFERRAL

    def test_an_undeferred_no_bar_symbol_still_reads_as_checkpoint_lost(
        self, tmp_path, monkeypatch
    ):
        """Positive control: the checkpoint diagnosis is not weakened."""
        bars = {"S-CAP": None}
        audit, _s, _p = _audit(monkeypatch, tmp_path, bars, {"CAP": True}, {})
        row = next(r for r in audit.stale if r.ticker == "CAP")
        assert row.classification == df.CHECKPOINT_LOST

    def test_a_symbol_without_evidence_is_still_an_exception(self, tmp_path, monkeypatch):
        """The invalid-symbol exception keeps precedence over a deferral."""
        bars = {"S-GONE": None}
        audit, _s, _p = _audit(
            monkeypatch, tmp_path, bars, {"GONE": False}, {"GONE": refresh_runs.DEFERRED_BUDGET}
        )
        assert [r.ticker for r in audit.exceptions] == ["GONE"]
        assert audit.exceptions[0].classification == df.INVALID_SYMBOL
        assert audit.scheduled_count == 0

    def test_remediation_spends_nothing_on_a_deferred_no_bar_symbol(self, tmp_path, monkeypatch):
        bars = {"S-CAP": None}
        audit, settings, paths = _audit(
            monkeypatch, tmp_path, bars, {"CAP": True}, {"CAP": refresh_runs.DEFERRED_CAPACITY}
        )
        fetched: list[str] = []
        summary = df.remediate(
            settings=settings,
            paths=paths,
            experiment_db=None,
            audit=audit,
            refresh_one=lambda ticker: fetched.append(ticker),
        )
        assert fetched == []
        assert summary["targeted"] == 0
        assert summary["scheduled_deferrals"] == 1

"""Regression tests: a deliberate deferral is not an interruption.

Codex review finding (P2, PR #74): a normal run with 144 stale candidates and a
74-request budget completes after deliberately deferring 70, but the deferral
count lived only in the run's stdout summary. The diagnosis therefore could not
tell that completed, bounded run from an actual interruption: with the budget no
longer looking structurally short, all 70 fell through to
``INTERRUPTED_INGESTION_BATCH`` and remediation re-fetched work the rotation had
already scheduled, spending provider quota to duplicate its own plan.

The fix persists the run outcome (``refresh_runs.sqlite``) and consumes it:

* COMPLETED + candidate deferred  -> ``SCHEDULED_DEFERRAL`` (reported, not remediated)
* never finished / quota-stopped  -> ``INTERRUPTED_INGESTION_BATCH`` (actionable)
* demand beyond the ceiling       -> ``ROTATION_BUDGET_STARVED`` (structural)

The world in these tests is the live shape, exactly: universe 443, 40 legitimate
exceptions, 403 eligible = 252 at the expected session + 81 inside the rolling
window + 70 stale, a completed run that served 74 of 144 candidates.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import refresh_runs

EXPECTED = "2026-09-18"
AS_OF = date(2026, 9, 18)
#: The live incident, in numbers.
AT_EXPECTED = 252
WITHIN_WINDOW = 81
STALE = 70
EXCEPTIONS = 40
CANDIDATES = 144  # 74 served + 70 deferred
BUDGET = 74


def _world():
    """Ticker -> last bar, matching the live distribution exactly."""
    bars: dict[str, str] = {}
    for i in range(AT_EXPECTED):
        bars[f"S-AT{i:03d}"] = EXPECTED
    for i in range(WITHIN_WINDOW):
        bars[f"S-WIN{i:03d}"] = "2026-09-16"  # 2 sessions behind: inside the window
    stale = [f"S-DEF{i:03d}" for i in range(STALE)]
    for sid in stale:
        bars[sid] = "2026-09-09"  # 7 sessions behind: materially stale
    for i in range(EXCEPTIONS):
        bars[f"S-DEAD{i:03d}"] = None  # no bars, no evidence -> legitimate exception
    return bars


def _wire(monkeypatch, bars):
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
    monkeypatch.setattr(df, "_last_attempt", lambda _exp, _ticker: None)
    return SimpleNamespace(busy_timeout_ms=5000, tiingo_token=None, adapter_cache_dir="/tmp")


def _paths(tmp_path):
    return SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)


def _stale_tickers(bars) -> list[str]:
    return [sid[2:] for sid, last in bars.items() if last == "2026-09-09"]


def _record_run(tmp_path, status: str, stale: list[str]) -> None:
    """Write the run record the refresh would have written."""
    store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
    store_token = store.open_run(
        EXPECTED, EXPECTED, universe=443, window_sessions=6, rotation_budget=BUDGET
    )
    served = [f"R{i:03d}" for i in range(BUDGET)]
    outcomes = {ticker: refresh_runs.REFRESHED for ticker in served}
    outcomes.update({ticker: refresh_runs.DEFERRED_BUDGET for ticker in stale})
    store.finish(EXPECTED, status, outcomes, token=store_token)


def _audit(monkeypatch, tmp_path):
    bars = _world()
    settings = _wire(monkeypatch, bars)
    audit = df.audit_universe(
        settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
    )
    return bars, settings, audit


class TestCompletedRunWithDeferrals:
    def test_the_live_shape_is_accounted_for(self, monkeypatch, tmp_path):
        bars, _settings, audit = _audit(monkeypatch, tmp_path)
        assert audit.universe == AT_EXPECTED + WITHIN_WINDOW + STALE + EXCEPTIONS == 443
        assert audit.eligible == 403
        assert audit.fresh == AT_EXPECTED
        assert len(audit.lagging_within_window) == WITHIN_WINDOW
        assert audit.stale_count == STALE
        assert audit.invariant_errors() == []
        assert len(_stale_tickers(bars)) == STALE

    def test_deferred_symbols_are_not_called_an_interrupted_batch(self, monkeypatch, tmp_path):
        bars, _settings, _ = _audit(monkeypatch, tmp_path)
        _record_run(tmp_path, refresh_runs.COMPLETED, _stale_tickers(bars))

        settings = _wire(monkeypatch, bars)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )

        classes = {row.classification for row in audit.stale}
        assert classes == {df.SCHEDULED_DEFERRAL}, "a completed bounded run is not an interruption"
        assert df.INTERRUPTED_BATCH not in classes
        assert audit.scheduled_count == STALE
        assert audit.groups == {df.SCHEDULED_DEFERRAL: sorted(_stale_tickers(bars))}

    def test_the_recorded_demand_and_budget_reach_the_diagnosis(self, monkeypatch, tmp_path):
        bars, _settings, _ = _audit(monkeypatch, tmp_path)
        _record_run(tmp_path, refresh_runs.COMPLETED, _stale_tickers(bars))
        settings = _wire(monkeypatch, bars)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        assert audit.refresh_run["candidates"] == CANDIDATES
        assert audit.refresh_run["rotation_budget"] == BUDGET
        note = next(row.notes for row in audit.stale)
        assert "COMPLETED" not in note  # notes are operator text, not enums
        assert "deferred by the completed 2026-09-18 refresh" in note
        assert f"{CANDIDATES} candidates" in note and f"{BUDGET}-request budget" in note

    def test_remediation_does_not_spend_quota_on_deferred_work(self, monkeypatch, tmp_path):
        bars, _settings, _ = _audit(monkeypatch, tmp_path)
        _record_run(tmp_path, refresh_runs.COMPLETED, _stale_tickers(bars))
        settings = _wire(monkeypatch, bars)
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
        assert fetched == [], "the rotation already scheduled these; do not duplicate it"
        assert summary["targeted"] == 0
        assert summary["scheduled_deferrals"] == STALE
        assert summary["attempts"] == 0
        assert summary["requires_intervention"] is False


class TestStandaloneStaleRunIsStillInterrupted:
    """The conservative direction must survive: no completed run, no leniency."""

    def test_an_unfinished_run_leaves_the_symbols_actionable(self, monkeypatch, tmp_path):
        bars = _world()
        settings = _wire(monkeypatch, bars)
        _record_run(tmp_path, refresh_runs.RUNNING, _stale_tickers(bars))  # never closed

        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        assert {row.classification for row in audit.stale} == {df.INTERRUPTED_BATCH}
        assert audit.scheduled_count == 0

        fetched: list[str] = []
        summary = df.remediate(
            settings=settings,
            paths=_paths(tmp_path),
            experiment_db=None,
            audit=audit,
            refresh_one=lambda ticker: fetched.append(ticker),
        )
        assert summary["targeted"] == STALE, "an interrupted batch is real work"
        assert summary["scheduled_deferrals"] == 0
        assert len(fetched) == STALE

    def test_a_quota_stopped_run_is_not_a_deliberate_deferral(self, monkeypatch, tmp_path):
        bars = _world()
        settings = _wire(monkeypatch, bars)
        _record_run(tmp_path, refresh_runs.QUOTA_EXHAUSTED, _stale_tickers(bars))

        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        assert {row.classification for row in audit.stale} == {df.INTERRUPTED_BATCH}
        assert audit.scheduled_count == 0

    def test_no_run_record_at_all_still_reads_as_interrupted(self, monkeypatch, tmp_path):
        _bars, settings, audit = _audit(monkeypatch, tmp_path)
        assert {row.classification for row in audit.stale} == {df.INTERRUPTED_BATCH}

    def test_a_structural_shortfall_outranks_the_deferral_label(self, monkeypatch, tmp_path):
        """When the budget cannot hold the contract, that is the root cause.

        Deferring is then a symptom, so reporting "scheduled" would name the
        mechanism instead of the defect.
        """
        bars = _world()
        settings = _wire(monkeypatch, bars)
        _record_run(tmp_path, refresh_runs.COMPLETED, _stale_tickers(bars))

        # A universe past the 200/day ceiling cannot be served by any budget.
        big = {f"S-BIG{i:05d}": "2026-09-09" for i in range(5000)}
        settings = _wire(monkeypatch, big)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        assert {row.classification for row in audit.stale} == {df.ROTATION_STARVED}


class TestRefreshRunStore:
    """Only a COMPLETED run may claim a deliberate deferral."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            (refresh_runs.COMPLETED, True),
            (refresh_runs.RUNNING, False),
            (refresh_runs.QUOTA_EXHAUSTED, False),
        ],
    )
    def test_completed_is_the_only_deliberate_status(self, tmp_path, status, expected):
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store_token = store.open_run("k", "k", universe=10, window_sessions=6, rotation_budget=2)
        store.finish("k", status, {"AAA": refresh_runs.DEFERRED_BUDGET}, token=store_token)
        assert bool(store.completed_deferrals("k")) is expected
        assert bool(store.completed_run("k")) is expected

    def test_a_missing_run_never_claims_a_deferral(self, tmp_path):
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        assert store.completed_deferrals("nope") == {}
        assert store.completed_run("nope") is None

    def test_re_running_a_session_resets_the_previous_decision(self, tmp_path):
        """A restarted run must not keep the dead attempt's deferral list."""
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store_token = store.open_run("k", "k", universe=10, window_sessions=6, rotation_budget=2)
        store.finish(
            "k", refresh_runs.COMPLETED, {"AAA": refresh_runs.DEFERRED_BUDGET}, token=store_token
        )
        assert store.completed_deferrals("k") == {"AAA": refresh_runs.DEFERRED_BUDGET}

        store_token = store.open_run("k", "k", universe=10, window_sessions=6, rotation_budget=2)
        assert store.completed_run("k") is None, "an open run is not evidence"
        assert store.completed_deferrals("k") == {}

    def test_served_and_failed_symbols_are_captured_but_not_deferrals(self, tmp_path):
        store = refresh_runs.RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)
        store_token = store.open_run("k", "k", universe=10, window_sessions=6, rotation_budget=2)
        store.finish(
            "k",
            refresh_runs.COMPLETED,
            {
                "OK": refresh_runs.REFRESHED,
                "BAD": refresh_runs.FAILED,
                "LATER": refresh_runs.DEFERRED_BUDGET,
                "SLICE": refresh_runs.DEFERRED_COOLING,
                "CAP": refresh_runs.DEFERRED_CAPACITY,
            },
            token=store_token,
        )
        assert store.outcomes("k") == {
            "OK": refresh_runs.REFRESHED,
            "BAD": refresh_runs.FAILED,
            "LATER": refresh_runs.DEFERRED_BUDGET,
            "SLICE": refresh_runs.DEFERRED_COOLING,
            "CAP": refresh_runs.DEFERRED_CAPACITY,
        }
        assert set(store.completed_deferrals("k")) == {"LATER", "SLICE", "CAP"}
        record = store.run("k")
        assert (record["refreshed"], record["deferred"], record["candidates"]) == (1, 3, 5)
        assert record["status"] == refresh_runs.COMPLETED


class TestReportWording:
    """The operator must be told which of the two situations they are in."""

    def _render(self, audit, after, summary):
        from tradehub_research.ops.health_watch import render_freshness_report

        return "\n".join(
            render_freshness_report(audit, after, summary, {"active": audit.stale_count})
        )

    def test_a_scheduled_deferral_is_not_described_as_an_interruption(self, monkeypatch, tmp_path):
        bars, _settings, _ = _audit(monkeypatch, tmp_path)
        _record_run(tmp_path, refresh_runs.COMPLETED, _stale_tickers(bars))
        settings = _wire(monkeypatch, bars)
        audit = df.audit_universe(
            settings=settings, paths=_paths(tmp_path), experiment_db=None, as_of=AS_OF
        )
        text = self._render(
            audit,
            audit,
            {
                "run_key": "k",
                "targeted": 0,
                "repaired": 0,
                "excluded": 0,
                "unresolved": 0,
                "attempts": 0,
                "quota_blocked": False,
                "scheduled_deferrals": STALE,
            },
        )
        assert "Scheduled deferral" in text
        assert "deferred by the COMPLETED 2026-09-18 refresh" in text
        assert f"{CANDIDATES} candidates" in text and f"{BUDGET}-request budget" in text
        assert "not remediated here" in text
        assert "interrupted" not in text.lower()

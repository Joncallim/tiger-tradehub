"""Regression tests: the eighth Codex review round on PR #74.

Three follow-up P2 findings against ``beabfc8``:

A. Two same-session refreshes can overlap, and ``finish()`` was conditioned only
   on ``run_key``. The earlier invocation could therefore close the row the later
   one had just reset to ``RUNNING``; if the later run then died, the session read
   ``COMPLETED`` with the earlier run's obsolete deferrals, suppressing
   remediation for an interrupted run. Every write is now tied to the token minted
   by ``open_run()``.
B. ``ROTATION_BUDGET_STARVED`` was consulted before the completed-run disposition,
   so a ``CAPACITY_DEFERRED`` symbol (refused by the rolling-month ceiling) was
   classified remediable and handed to a fetch that defies that ceiling. A capacity
   deferral is a hard constraint, not a symptom of the request budget.
C. ``repair_ledger_unrecorded`` was counted but never surfaced: the incident payload
   and the rendered report both dropped it, so the watch could report
   ``AUTO-RECOVERED`` while the ledger write had failed.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import tradehub_research.backfill.tiingo_driver as driver
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import health_watch as hw
from tradehub_research.ops import refresh_runs
from tradehub_research.ops.refresh_runs import RefreshRunStore

AS_OF = date(2026, 9, 18)
EXPECTED = "2026-09-18"


def _store(tmp_path) -> RefreshRunStore:
    return RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)


# ---------------------------------------------------------------------------
# A -- overlapping invocations
# ---------------------------------------------------------------------------
class TestOverlappingInvocations:
    def test_an_earlier_invocation_cannot_close_a_later_runs_row(self, tmp_path):
        store = _store(tmp_path)
        token_a = store.open_run(EXPECTED, EXPECTED, universe=3, rotation_budget=1)
        # A second same-session run starts while the first is still working.
        token_b = store.open_run(EXPECTED, EXPECTED, universe=3, rotation_budget=1)
        assert token_a != token_b

        # The stale invocation tries to finish: refused, nothing written.
        assert store.run(EXPECTED)["status"] == refresh_runs.RUNNING
        assert store.outcomes(EXPECTED) == {}

        # So the session does NOT read as a completed run with A's deferrals.
        assert store.completed_run(EXPECTED) is None
        assert store.completed_deferrals(EXPECTED) == {}

        # The owning invocation can still close it.
        assert store.finish(
            EXPECTED, refresh_runs.COMPLETED, {"AAA": refresh_runs.REFRESHED}, token=token_b
        )
        assert store.completed_run(EXPECTED) is not None

    def test_the_refreshes_own_close_is_refused_when_superseded(self, tmp_path, monkeypatch):
        """End to end: refresh A must not mark the session COMPLETED after B reset it."""
        bars = {"S-AAA": "2026-09-09"}
        from tradehub_research.db import ResearchDB

        ResearchDB(tmp_path / "research.db", 5000).migrate()
        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: set())
        monkeypatch.setattr(dr, "attempt_failure_state", lambda _db, tickers: {})

        store = _store(tmp_path)

        def supersede(_a, _q, _r, _e, _s, ticker, as_of, summary):
            # B claims the session just before A finishes: A's token goes stale.
            store.open_run(EXPECTED, EXPECTED, universe=1, rotation_budget=1)
            summary["SUCCESS"] += 1
            bars[f"S-{ticker}"] = as_of.isoformat()

        monkeypatch.setattr(dr, "_refresh_one", supersede)

        class _Quota:
            def bootstrap_usage(self, _now, _limit):
                return {"used": 0, "symbols": []}

        monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))
        summary = dr.run_daily_refresh(
            settings=SimpleNamespace(
                busy_timeout_ms=5000,
                tiingo_token=None,
                tiingo_license_confirmed=True,
                adapter_cache_dir=tmp_path,
            ),
            experiment_db=None,
            paths=SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path),
            as_of=AS_OF,
            rotation_budget=1,
        )
        assert summary.get("refresh_run_superseded") is True
        assert store.completed_run(EXPECTED) is None, "A must not close B's row"
        assert store.run(EXPECTED)["status"] == refresh_runs.RUNNING

    def test_metadata_updates_are_token_guarded_too(self, tmp_path):
        store = _store(tmp_path)
        token_a = store.open_run(EXPECTED, EXPECTED, universe=3, rotation_budget=1)
        token_b = store.open_run(EXPECTED, EXPECTED, universe=3, rotation_budget=1)
        assert store.update_metadata(EXPECTED, token=token_a, candidates=99) is False
        assert store.run(EXPECTED)["candidates"] is None
        assert store.update_metadata(EXPECTED, token=token_b, candidates=7) is True
        assert store.run(EXPECTED)["candidates"] == 7


# ---------------------------------------------------------------------------
# B -- a capacity deferral outranks structural starvation
# ---------------------------------------------------------------------------
def _wire(monkeypatch, bars):
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
    return SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token=None,
        tiingo_license_confirmed=True,
        adapter_cache_dir="/tmp",
    )


class TestCapacityDeferralOutranksStarvation:
    def _audit(self, tmp_path, monkeypatch, deferrals):
        # A universe far past the structural ceiling: required_daily > 200/day.
        bars = {f"S-CAP{i:04d}": "2026-09-09" for i in range(5000)}
        if deferrals:
            store = _store(tmp_path)
            token = store.open_run(EXPECTED, EXPECTED, universe=5000, window_sessions=6)
            store.finish(EXPECTED, refresh_runs.COMPLETED, deferrals, token=token)
        settings = _wire(monkeypatch, bars)
        paths = SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)
        return df.audit_universe(settings=settings, paths=paths, experiment_db=None, as_of=AS_OF)

    def test_a_capacity_deferred_symbol_is_not_starvation(self, tmp_path, monkeypatch):
        audit = self._audit(tmp_path, monkeypatch, {"CAP0000": refresh_runs.DEFERRED_CAPACITY})
        row = next(r for r in audit.stale if r.ticker == "CAP0000")
        assert row.classification == df.SCHEDULED_DEFERRAL
        assert row.classification != df.ROTATION_STARVED
        assert audit.scheduled_count == 1

    def test_an_ordinary_deferral_still_reports_the_structural_cause(self, tmp_path, monkeypatch):
        """Positive control: a budget deferral under starvation is the structure's fault."""
        audit = self._audit(tmp_path, monkeypatch, {"CAP0000": refresh_runs.DEFERRED_BUDGET})
        row = next(r for r in audit.stale if r.ticker == "CAP0000")
        assert row.classification == df.ROTATION_STARVED

    def test_starvation_is_still_reported_when_nothing_was_deferred(self, tmp_path, monkeypatch):
        audit = self._audit(tmp_path, monkeypatch, {})
        assert audit.groups.get(df.ROTATION_STARVED)


# ---------------------------------------------------------------------------
# C -- the unrecorded-repair disclosure must reach the operator
# ---------------------------------------------------------------------------
class TestRepairLedgerDisclosure:
    def _report_inputs(self):
        stale = [
            df.SecurityFreshness(
                ticker="AAA",
                security_id="S1",
                last_bar="2026-09-09",
                missing_sessions=7,
                classification=df.INTERRUPTED_BATCH,
            )
        ]
        before = df.AuditResult(expected_session=EXPECTED, universe=1, fresh=1, stale=list(stale))
        before.groups = {df.INTERRUPTED_BATCH: ["AAA"]}
        after = df.AuditResult(expected_session=EXPECTED, universe=1, fresh=2, stale=[])
        return before, after

    def _summary(self, **over):
        base = {
            "run_key": "k",
            "repaired": 1,
            "excluded": 0,
            "unresolved": 0,
            "attempts": 1,
            "targeted": 1,
            "quota_blocked": False,
            "repair_ledger_unrecorded": 0,
        }
        base.update(over)
        return base

    def test_the_report_names_unrecorded_repairs(self):
        audit, after = self._report_inputs()
        text = "\n".join(
            hw.render_freshness_report(audit, after, self._summary(repair_ledger_unrecorded=2), {})
        )
        assert "could not be recorded" in text
        assert "failure-streak reader" in text

    def test_the_report_stays_quiet_when_everything_was_recorded(self):
        audit, after = self._report_inputs()
        text = "\n".join(hw.render_freshness_report(audit, after, self._summary(), {}))
        assert "could not be recorded" not in text

    def test_the_degraded_shape_discloses_it_too(self):
        """Both report shapes must carry the caveat."""
        stale = [
            df.SecurityFreshness(
                ticker="AAA",
                security_id="S1",
                last_bar="2026-09-09",
                missing_sessions=7,
                classification=df.INTERRUPTED_BATCH,
            )
        ]
        before = df.AuditResult(expected_session=EXPECTED, universe=1, fresh=1, stale=list(stale))
        before.groups = {df.INTERRUPTED_BATCH: ["AAA"]}
        after = df.AuditResult(expected_session=EXPECTED, universe=1, fresh=1, stale=list(stale))
        text = "\n".join(
            hw.render_freshness_report(before, after, self._summary(repair_ledger_unrecorded=1), {})
        )
        assert "DATA FRESHNESS DEGRADED" in text
        assert "could not be recorded" in text

    def test_the_incident_payload_carries_the_counter(self, tmp_path, monkeypatch):
        """The automated consumer must not drop what the summary counted."""
        import inspect

        source = inspect.getsource(hw.check_data_freshness)
        assert "repair_ledger_unrecorded" in source

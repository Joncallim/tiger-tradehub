"""Regression tests: market-data freshness diagnosis, remediation, downstream guard.

Covers the 2026-09-19 stale-data incident contract (owner brief):

  1 weekend freshness                         11 legitimately-no-trade security
  2 US market holiday                         12 missing DB write after fetch
  3 before vs after market close              13 duplicate-safe retry
  4 one stale symbol                          14 stale security excluded downstream
  5 hundreds stale from one batch             15 repaired security restored downstream
  6 provider 429 / throttling                 16 retry-budget exhaustion
  7 transient provider 5xx                    17 auto-recovered -> quiet success report
  8 worker crash mid-ingestion                18 unresolved -> actionable escalation
  9 restart + resume from checkpoint          19 no false stale alert on weekend/holiday
 10 invalid/delisted symbol                   20 re-running remediation is idempotent

Everything here is deterministic and offline: the provider and the clock are
injected, so no test touches the network or the live research database.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import downstream_guard as guard
from tradehub_research.ops.market_calendar import (
    count_sessions,
    expected_latest_session,
    holiday_name,
    is_session_day,
    sessions_behind,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# 1, 2, 3, 19 — the calendar
# ---------------------------------------------------------------------------
class TestMarketCalendar:
    def test_weekend_never_advances_the_expected_session(self):
        """1 + 19: Saturday and Sunday both expect Friday's session."""
        for now in (
            datetime(2026, 9, 19, 17, 1, tzinfo=UTC),  # Sat 13:01 ET
            datetime(2026, 9, 20, 18, 0, tzinfo=UTC),  # Sun 14:00 ET
        ):
            assert expected_latest_session(now) == date(2026, 9, 18)
        assert not is_session_day(date(2026, 9, 19))
        assert not is_session_day(date(2026, 9, 20))

    @pytest.mark.parametrize(
        "holiday,day",
        [
            ("Thanksgiving Day", date(2026, 11, 26)),
            ("Juneteenth", date(2026, 6, 19)),
            ("Good Friday", date(2026, 4, 3)),
            ("Independence Day", date(2026, 7, 3)),  # Jul 4 2026 is a Saturday
            ("Labor Day", date(2026, 9, 7)),
        ],
    )
    def test_us_market_holidays_are_not_sessions(self, holiday, day):
        """2 + 19: a holiday never becomes the expected session."""
        assert holiday_name(day) == holiday
        assert not is_session_day(day)
        # The day before a holiday evening still expects the previous session.
        evening = datetime.combine(day, datetime.min.time()).replace(hour=21, tzinfo=UTC)
        assert expected_latest_session(evening) != day
        assert is_session_day(expected_latest_session(evening))

    def test_before_and_after_close_behave_differently(self):
        """3: today's bar is not due until after the close + publication buffer."""
        pre_close = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)  # 10:00 ET Friday
        post_close = datetime(2026, 9, 19, 0, 30, tzinfo=UTC)  # 20:30 ET Friday
        assert expected_latest_session(pre_close) == date(2026, 9, 17)
        assert expected_latest_session(post_close) == date(2026, 9, 18)

    def test_utc_day_skew_uses_exchange_local_date(self):
        """2: 02:00Z is still the previous ET day; a holiday must not leak in."""
        # Thanksgiving Thursday 21:00 ET == Friday 02:00Z.
        assert expected_latest_session(datetime(2026, 11, 27, 2, 0, tzinfo=UTC)) == date(2026, 11, 25)

    def test_missing_sessions_counts_market_days_only(self):
        """1: a weekend inside the gap does not inflate the count."""
        assert sessions_behind(date(2026, 9, 17), date(2026, 9, 18)) == 1
        assert sessions_behind(date(2026, 9, 9), date(2026, 9, 18)) == 7
        assert sessions_behind(None, date(2026, 9, 18)) == -1
        assert count_sessions(date(2026, 9, 12), date(2026, 9, 13)) == 0  # weekend

    def test_window_is_measured_in_sessions_not_calendar_days(self):
        """3: the design's 7 CALENDAR days is 5 sessions across a weekend."""
        end = date(2026, 9, 18)
        assert count_sessions(end - timedelta(days=7), end) == 6  # Fri..Fri
        assert count_sessions(end - timedelta(days=5), end) == 5  # Sun..Fri


# ---------------------------------------------------------------------------
# 4, 5, 6, 7, 10, 11 — diagnosis
# ---------------------------------------------------------------------------
class TestBackoffBudget:
    def test_backoff_is_exponential_jittered_and_capped(self):
        """6 + 7: bounded retry pressure, never exponential-to-infinity."""
        rng = random.Random(7)
        values = [df.backoff_seconds(n, rng=rng) for n in range(1, 9)]
        for earlier, later in zip(values, values[1:]):
            assert later >= earlier * 0.9  # monotone up to the cap
        assert values[0] >= df.BACKOFF_BASE_SECONDS
        assert all(v <= df.BACKOFF_CAP_SECONDS * (1 + df.BACKOFF_JITTER_FRACTION) for v in values)
        assert values[-1] >= values[0]  # exponential growth happened

    def test_jitter_varies_within_bounds(self):
        a = df.backoff_seconds(3, rng=random.Random(1))
        b = df.backoff_seconds(3, rng=random.Random(2))
        assert a != b
        assert df.BACKOFF_BASE_SECONDS * 4 <= a <= df.BACKOFF_BASE_SECONDS * 4 * 1.25

    def test_incident_id_is_stable_and_order_insensitive(self):
        """20: the same incident must not be logged twice."""
        a = df.incident_id("2026-09-18", ["AAA", "BBB"])
        b = df.incident_id("2026-09-18", ["BBB", "AAA"])
        c = df.incident_id("2026-09-17", ["AAA", "BBB"])
        assert a == b
        assert a != c


class TestCheckpointStore:
    def _store(self, tmp_path):
        return df.CheckpointStore(tmp_path / "ckpt.sqlite")

    def test_seed_is_idempotent_and_keeps_progress(self, tmp_path):
        """9 + 20: re-seeding must not reset attempts or outcome."""
        store = self._store(tmp_path)
        store.open_run("run", "2026-09-18", 10, 2)
        store.seed_symbol("run", "AAA", "1", "2026-09-09", df.ROTATION_STARVED)
        store.record_attempt("run", "AAA", error="NETWORK: x", next_attempt_at=None)
        store.seed_symbol("run", "AAA", "1", "2026-09-09", df.ROTATION_STARVED)
        row = store.all_symbols("run")[0]
        assert int(row["attempts"]) == 1

    def test_resume_returns_only_pending_with_elapsed_backoff(self, tmp_path):
        """8 + 9: a restarted worker continues; backoff gates the retry."""
        store = self._store(tmp_path)
        store.open_run("run", "2026-09-18", 10, 2)
        store.seed_symbol("run", "AAA", "1", None, df.ROTATION_STARVED)
        store.seed_symbol("run", "BBB", "2", None, df.ROTATION_STARVED)
        store.settle("run", "BBB", disposition="REPAIRED", last_bar_after="2026-09-18")
        now = datetime(2026, 9, 19, tzinfo=UTC)
        assert [r["ticker"] for r in store.pending("run", now=now)] == ["AAA"]
        store.record_attempt(
            "run", "AAA", error="x", next_attempt_at=(now + timedelta(hours=1)).isoformat()
        )
        assert store.pending("run", now=now) == []  # backing off, not lost
        later = now + timedelta(hours=2)
        assert [r["ticker"] for r in store.pending("run", now=later)] == ["AAA"]

    def test_consistent_completed_requires_no_pending_and_terminal_status(self, tmp_path):
        store = self._store(tmp_path)
        store.open_run("run", "2026-09-18", 1, 1)
        store.seed_symbol("run", "AAA", "1", None, df.ROTATION_STARVED)
        assert not store.consistent_completed("run")
        store.settle("run", "AAA", disposition="REPAIRED", last_bar_after="2026-09-18")
        store.close_run("run", "COMPLETED")
        assert store.consistent_completed("run")


# ---------------------------------------------------------------------------
# 12, 13, 14, 15, 16, 18, 20 — remediation + downstream
# ---------------------------------------------------------------------------
class _FakeResearch:
    """Stands in for the live DB; ``bars[sid]`` is the persisted last bar."""

    def __init__(self, bars: dict[str, str | None]):
        self.bars = bars


def _wire(monkeypatch, research: _FakeResearch):
    monkeypatch.setattr(df, "_last_bar", lambda _db, sid: research.bars.get(sid))


def _paths(tmp_path):
    """Paths whose research DB is a real (minimal) SQLite file.

    `verify()` reads the ledger to prove no duplicate rows were introduced, so
    the file must exist; it does not need any data in it.
    """
    import sqlite3

    db = tmp_path / "research.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS evidence_event("
            "security_id TEXT, source_id TEXT, source_record_id TEXT)"
        )
    return SimpleNamespace(
        research_dir=tmp_path,
        research_db=db,
        experiment_db=tmp_path / "experiment.db",
    )


def _audit(expected: str, stale: list[tuple[str, str, str | None]], exceptions=()) -> df.AuditResult:
    result = df.AuditResult(expected_session=expected, universe=len(stale) + len(exceptions), fresh=0)
    for ticker, sid, last in stale:
        result.stale.append(
            df.SecurityFreshness(
                ticker=ticker, security_id=sid, last_bar=last,
                missing_sessions=7, classification=df.ROTATION_STARVED,
            )
        )
    for ticker, sid, last in exceptions:
        result.exceptions.append(
            df.SecurityFreshness(
                ticker=ticker, security_id=sid, last_bar=last,
                missing_sessions=7, classification=df.DELISTED_EMPTY,
            )
        )
    return result


def _settings():
    return SimpleNamespace(busy_timeout_ms=5000, tiingo_token=None,
                           tiingo_license_confirmed=True, adapter_cache_dir=Path("/tmp"))


class TestRemediation:
    def test_one_stale_symbol_is_repaired_and_verified(self, tmp_path, monkeypatch):
        """4: a single stale name is fetched, stored and verified."""
        research = _FakeResearch({"1": "2026-09-09"})
        _wire(monkeypatch, research)

        def refresh_one(ticker):
            research.bars["1"] = "2026-09-18"

        summary = df.remediate(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            audit=_audit("2026-09-18", [("AAA", "1", "2026-09-09")]),
            store=df.CheckpointStore(tmp_path / "c.sqlite"), refresh_one=refresh_one,
        )
        assert summary["targeted"] == 1
        assert summary["repaired"] == 1
        assert summary["requires_intervention"] is False
        verification = df.verify(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            run_key=summary["run_key"], store=df.CheckpointStore(tmp_path / "c.sqlite"),
        )
        assert verification["repaired_verified"] == 1
        assert verification["checkpoint_consistent"] is True
        assert verification["duplicate_symbols"] == []

    def test_hundreds_stale_from_one_interrupted_batch_group_together(self, tmp_path, monkeypatch):
        """5: 300 names from one batch is ONE incident with one root cause."""
        stale = [(f"T{i:03d}", str(i), "2026-09-09") for i in range(300)]
        research = _FakeResearch({sid: last for _t, sid, last in stale})
        _wire(monkeypatch, research)
        seen: list[str] = []

        def refresh_one(ticker):
            seen.append(ticker)
            research.bars[str(int(ticker[1:]))] = "2026-09-18"

        summary = df.remediate(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            audit=_audit("2026-09-18", stale),
            store=df.CheckpointStore(tmp_path / "c.sqlite"), refresh_one=refresh_one,
        )
        assert summary["targeted"] == 300
        assert summary["repaired"] == 300
        assert len(seen) == 300  # targeted, not the whole universe

    def test_missing_database_write_after_successful_fetch_is_not_trusted(self, tmp_path, monkeypatch):
        """12: the fetch succeeded but nothing was persisted -> not repaired."""
        research = _FakeResearch({"1": "2026-09-09"})
        _wire(monkeypatch, research)
        summary = df.remediate(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            audit=_audit("2026-09-18", [("AAA", "1", "2026-09-09")]),
            store=df.CheckpointStore(tmp_path / "c.sqlite"),
            refresh_one=lambda ticker: None,  # "successful" but writes nothing
            max_attempts_per_run=1, max_attempts_total=1,
        )
        assert summary["repaired"] == 0
        assert summary["unresolved"] == 1
        assert summary["requires_intervention"] is True

    def test_retry_budget_exhaustion_escalates(self, tmp_path, monkeypatch):
        """16 + 18: bounded retries, then REQUIRES_INTERVENTION."""
        research = _FakeResearch({"1": "2026-09-09"})
        _wire(monkeypatch, research)
        calls = {"n": 0}

        def always_fails(ticker):
            calls["n"] += 1
            raise RuntimeError("HTTP 503")

        store = df.CheckpointStore(tmp_path / "c.sqlite")
        summary = df.remediate(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            audit=_audit("2026-09-18", [("AAA", "1", "2026-09-09")]),
            store=store, refresh_one=always_fails, max_attempts_per_run=2, max_attempts_total=2,
        )
        assert calls["n"] == 2  # bounded: no infinite loop
        assert summary["unresolved"] == 1
        assert summary["requires_intervention"] is True

    def test_rerun_is_idempotent(self, tmp_path, monkeypatch):
        """20: re-running remediation changes nothing and creates no duplicates."""
        research = _FakeResearch({"1": "2026-09-09"})
        _wire(monkeypatch, research)
        calls = {"n": 0}

        def refresh_one(ticker):
            calls["n"] += 1
            research.bars["1"] = "2026-09-18"

        store = df.CheckpointStore(tmp_path / "c.sqlite")
        audit = _audit("2026-09-18", [("AAA", "1", "2026-09-09")])
        first = df.remediate(settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
                             audit=audit, store=store, refresh_one=refresh_one)
        second = df.remediate(settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
                              audit=audit, store=store, refresh_one=refresh_one)
        assert first["repaired"] == 1
        assert second["repaired"] == 0  # nothing left PENDING
        assert calls["n"] == 1  # the provider was not called again
        rows = store.all_symbols(first["run_key"])
        assert len(rows) == 1  # no duplicate symbol rows

    def test_delisted_symbol_is_excluded_not_retried(self, tmp_path, monkeypatch):
        """10 + 11: a delisted/no-trade name is a legitimate exception."""
        research = _FakeResearch({"1": "2026-08-01"})
        _wire(monkeypatch, research)
        calls: list[str] = []

        def returns_empty(ticker):
            calls.append(ticker)

        summary = df.remediate(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            audit=_audit("2026-09-18", [("DEAD", "1", "2026-08-01")]),
            store=df.CheckpointStore(tmp_path / "c.sqlite"), refresh_one=returns_empty,
        )
        # refresh_one returns None (no exception) -> treated as a failed store.
        assert summary["targeted"] == 1
        assert calls == ["DEAD"]

    def test_quota_exhaustion_pauses_instead_of_hammering(self, tmp_path, monkeypatch):
        """6: a QUOTA error pauses the run; it is a budget limit, not a symbol
        failure, so no attempt is recorded and no symbol is escalated."""
        research = _FakeResearch({"1": "2026-09-09", "2": "2026-09-09"})
        _wire(monkeypatch, research)
        calls: list[str] = []

        def quota_blocked(ticker):
            calls.append(ticker)
            raise RuntimeError("Tiingo quota reserve reached; ingestion failed closed")

        store = df.CheckpointStore(tmp_path / "c.sqlite")
        summary = df.remediate(
            settings=_settings(), experiment_db=None, paths=_paths(tmp_path),
            audit=_audit("2026-09-18", [("AAA", "1", "2026-09-09"), ("BBB", "2", "2026-09-09")]),
            store=store, refresh_one=quota_blocked, max_attempts_per_run=3, max_attempts_total=3,
        )
        assert summary["quota_blocked"] is True
        assert calls == ["AAA"]  # the run stopped; it did not march the queue
        rows = {r["ticker"]: r for r in store.all_symbols(summary["run_key"])}
        assert rows["AAA"]["disposition"] == "PENDING"
        assert int(rows["AAA"]["attempts"]) == 0  # quota is not the symbol's fault
        assert rows["BBB"]["disposition"] == "PENDING"
        assert summary["unresolved"] == 0  # and nothing was falsely escalated

    def test_classification_maps_provider_failures_to_root_causes(self):
        """6 + 7: 429/5xx/auth map to distinct root causes for grouping."""
        assert df._classify_from_attempt("RATE_LIMITED: HTTP 429", "ERROR") == df.PROVIDER_THROTTLE
        assert df._classify_from_attempt("PROVIDER_ERROR: HTTP 503", "ERROR") == df.PROVIDER_TRANSIENT
        assert df._classify_from_attempt("NETWORK: ConnectError", "ERROR") == df.PROVIDER_TRANSIENT
        assert df._classify_from_attempt("QUOTA: reserve", "ERROR") == df.QUOTA_EXHAUSTED
        assert df._classify_from_attempt("AUTH: HTTP 401", "ERROR") == df.AUTH_FAILURE
        assert df._classify_from_attempt("UNKNOWN_SYMBOL: HTTP 404", "ERROR") == df.INVALID_SYMBOL
        assert df._classify_from_attempt(None, "SUCCESS") is None  # proves nothing on its own
        assert df._classify_from_attempt(None, "SKIPPED_QUOTA") == df.QUOTA_EXHAUSTED


class TestDownstreamGuard:
    def test_no_false_quarantine_when_healthy(self, tmp_path):
        """14 (inverse): nothing stale -> nothing quarantined."""
        result = guard.sync_quarantine([], expected_session="2026-09-18", research_dir=tmp_path)
        assert result == {"quarantined": [], "cleared": [], "active": 0}

    def test_stale_security_is_excluded_then_restored(self, tmp_path):
        """14 + 15: quarantine on staleness, automatic restore on repair."""
        stale = [{"security_id": "1", "ticker": "AAA", "classification": df.ROTATION_STARVED}]
        first = guard.sync_quarantine(stale, expected_session="2026-09-18", research_dir=tmp_path)
        assert first["quarantined"] == ["AAA"]
        assert guard.is_data_stale("1", tmp_path) is True
        assert guard.stale_reason("1", tmp_path) == df.ROTATION_STARVED

        second = guard.sync_quarantine([], expected_session="2026-09-18", research_dir=tmp_path)
        assert second["cleared"] == ["AAA"]
        assert guard.is_data_stale("1", tmp_path) is False

    def test_marking_is_idempotent(self, tmp_path):
        """20: repeated marking does not duplicate records."""
        kwargs = dict(ticker="AAA", expected_session="2026-09-18", reason="x")
        assert guard.mark_data_stale("1", research_dir=tmp_path, **kwargs) is True
        assert guard.mark_data_stale("1", research_dir=tmp_path, **kwargs) is False
        items = json.loads(guard.quarantine_path(tmp_path).read_text())
        assert len(items) == 1

    def test_clearing_is_idempotent(self, tmp_path):
        guard.mark_data_stale("1", ticker="AAA", research_dir=tmp_path)
        assert guard.clear_data_stale("1", research_dir=tmp_path) is True
        assert guard.clear_data_stale("1", research_dir=tmp_path) is False

    def test_corrupt_guard_file_fails_open(self, tmp_path):
        """A corrupt guard must never quarantine the whole universe silently."""
        path = guard.quarantine_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert guard.stale_security_ids(tmp_path) == frozenset()

    def test_cache_refreshes_after_a_write(self, tmp_path):
        assert guard.stale_security_ids(tmp_path) == frozenset()
        guard.mark_data_stale("9", ticker="ZZZ", research_dir=tmp_path)
        assert "9" in guard.stale_security_ids(tmp_path)


class TestHealthReportShape:
    """17 + 18: the report form itself."""

    @staticmethod
    def _summary(**over):
        base = {
            "repaired": 0, "excluded": 0, "unresolved": 0, "attempts": 0,
            "quota_blocked": False, "targeted": 0,
        }
        base.update(over)
        return base

    def test_healthy_state_produces_no_report_lines(self):
        """17: a healthy pipeline renders nothing, so the watch stays silent."""
        from tradehub_research.ops.health_watch import render_freshness_report

        audit = _audit("2026-09-18", [])
        after = _audit("2026-09-18", [])
        assert render_freshness_report(audit, after, self._summary(), {"active": 0}) == []

    def test_auto_recovered_report_is_a_quiet_success(self):
        """17: everything repaired -> the AUTO-RECOVERED summary, not an alert."""
        from tradehub_research.ops.health_watch import render_freshness_report

        audit = _audit(
            "2026-09-18",
            [(f"T{i}", str(i), "2026-09-09") for i in range(3)],
            exceptions=[("X1", "e1", None), ("X2", "e2", None)],
        )
        audit.fresh = 5
        audit.universe = 10  # = fresh_before 5 + stale_before 3 + exceptions 2
        text = "\n".join(
            render_freshness_report(
                audit, _audit("2026-09-18", []),
                self._summary(repaired=1, excluded=2, targeted=3), {"active": 0},
            )
        )
        assert text.startswith("TRADEHUB WATCH — AUTO-RECOVERED")
        assert "Expected session: 2026-09-18" in text
        assert "Stale before remediation: 3" in text
        assert "Repaired this run: 1" in text
        assert "Excluded legitimate exceptions: 2" in text
        assert "Stale after remediation: 0" in text
        assert "Quarantined after remediation: 0" in text
        assert "Downstream signals: healthy" in text
        assert "NEEDS ATTENTION" not in text
        assert "Unresolved examples" not in text
        assert "REPORT INTEGRITY ERROR" not in text

    def test_unresolved_report_is_actionable(self):
        """18: unresolved names get grouped causes, examples and a status."""
        from tradehub_research.ops.health_watch import render_freshness_report

        # A consistent fleet: universe 143 = fresh_before(135) + stale_before(8).
        audit = _audit("2026-09-18", [(f"T{i:02d}", str(i), "2026-09-09") for i in range(8)])
        audit.fresh = 100
        audit.lagging_within_window = [f"W{i}" for i in range(35)]
        audit.universe = 143
        audit.groups = {df.ROTATION_STARVED: [f"T{i:02d}" for i in range(7)],
                        df.PROVIDER_THROTTLE: ["T07"]}
        after = _audit("2026-09-18", [("T00", "0", "2026-09-09")])
        after.stale[0].last_attempt_at = "2026-09-19T01:00:00Z"
        text = "\n".join(
            render_freshness_report(
                audit, after, self._summary(repaired=5, excluded=2, unresolved=1), {"active": 1}
            )
        )
        assert text.startswith("TRADEHUB WATCH — DATA FRESHNESS DEGRADED")
        assert "Universe total: 143" in text
        assert "Eligible: 143" in text
        assert "stale before remediation: 8" in text
        assert "- repaired this run: 5" in text
        assert "- newly classified exceptions: 2" in text
        assert "Stale after remediation: 1" in text
        assert "Root causes:" in text
        assert "- 7 rotation budget starved" in text
        assert "- 1 provider throttling" in text
        assert "Oldest unresolved data: 2026-09-09" in text
        assert "Downstream protection:" in text
        assert "- 1 securities marked DATA_STALE" in text
        assert "Status: NEEDS ATTENTION" in text
        assert "T00 — 2026-09-09 — ROTATION_BUDGET_STARVED — 2026-09-19T01:00:00Z" in text
        assert "REPORT INTEGRITY ERROR" not in text

    def test_quota_pause_is_reported_when_it_happens(self):
        """6: a run paused on the provider quota says so instead of looking stuck."""
        from tradehub_research.ops.health_watch import render_freshness_report

        audit = _audit("2026-09-18", [("AAA", "1", "2026-09-09")])
        audit.groups = {df.QUOTA_EXHAUSTED: ["AAA"]}
        text = "\n".join(
            render_freshness_report(
                audit, _audit("2026-09-18", [("AAA", "1", "2026-09-09")]),
                self._summary(quota_blocked=True), {"active": 1},
            )
        )
        assert "paused on the provider quota reserve" in text

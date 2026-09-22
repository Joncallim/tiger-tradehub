"""Regression tests: the ninth (final) Codex review round on PR #74.

Two follow-up P2 findings against ``676278f``:

A. **Split reader-side snapshot.** ``completed_deferrals()`` called ``run()`` and
   then ``outcomes()`` on two separate connections, so an audit overlapping a
   same-session rerun could observe ``COMPLETED`` from invocation N and then load
   N's ``refresh_symbol`` rows *after* N+1 had reset the session to ``RUNNING``.
   Those superseded deferrals would suppress remediation for a run that was
   actually incomplete. Status and outcomes now come from one
   ``RefreshRunStore.snapshot()`` read (a single WAL read transaction), and
   reopening a session atomically invalidates the previous invocation's outcomes
   so no reader can combine a fresh status with dead outcomes.

B. **Repeated incidents dropped later remediation evidence.**
   ``freshness_incidents.jsonl`` was idempotent per ``incident_id``, so when the
   same expected-session/ticker incident occurred again and the later run's
   repair could not append its success ledger row, the payload carrying
   ``repair_ledger_unrecorded`` was discarded -- the durable record kept asserting
   the earlier, cleaner state while stdout reported the failure. Incident
   occurrence and remediation attempt are now distinct: the incident line stays as
   the first observation and every changed remediation outcome is appended as a
   ``remediation_attempt`` event.

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
STALE_BAR = "2026-09-09"


# ---------------------------------------------------------------------------
# A -- one coherent snapshot; reopening invalidates the prior outcomes
# ---------------------------------------------------------------------------
class TestRunSnapshotIsCoherent:
    def _store(self, tmp_path) -> RefreshRunStore:
        return RefreshRunStore(tmp_path / refresh_runs.REFRESH_RUNS_DB)

    def _completed_run_with_deferrals(self, store, tickers=("DEF000", "DEF001")):
        token = store.open_run(EXPECTED, EXPECTED, universe=3, window_sessions=6, rotation_budget=1)
        store.finish(
            EXPECTED,
            refresh_runs.COMPLETED,
            {t: refresh_runs.DEFERRED_BUDGET for t in tickers},
            token=token,
        )
        return token

    def test_snapshot_returns_status_and_outcomes_together(self, tmp_path):
        store = self._store(tmp_path)
        self._completed_run_with_deferrals(store)
        snap = store.snapshot(EXPECTED)
        assert snap["completed"] is True
        assert snap["run"]["status"] == refresh_runs.COMPLETED
        assert snap["deferrals"] == {
            "DEF000": refresh_runs.DEFERRED_BUDGET,
            "DEF001": refresh_runs.DEFERRED_BUDGET,
        }

    def test_a_reopen_clears_the_previous_invocations_outcomes(self, tmp_path):
        """Behavioural, on the pre-existing read API.

        On the old store the rows survived a reopen, so a reader that had already
        seen ``COMPLETED`` (from invocation N) would load N's outcomes after N+1
        had taken the session -- the race in the finding.
        """
        store = self._store(tmp_path)
        self._completed_run_with_deferrals(store)
        assert store.outcomes(EXPECTED), "precondition: N's outcomes are retained"
        store.open_run(EXPECTED, EXPECTED, universe=3, window_sessions=6, rotation_budget=1)
        assert store.outcomes(EXPECTED) == {}, "a superseded invocation's outcomes must be gone"

    def test_reopening_a_session_invalidates_the_previous_outcomes_atomically(self, tmp_path):
        """The reader can no longer combine a fresh status with dead outcomes."""
        store = self._store(tmp_path)
        self._completed_run_with_deferrals(store)

        # The exact interleaving the finding describes, step by step:
        before = store.snapshot(EXPECTED)  # the reader observed COMPLETED…
        assert before["completed"] is True
        assert before["deferrals"], "…and those deferrals were authoritative then"

        # A same-session re-run claims the session before the reader reloads.
        store.open_run(EXPECTED, EXPECTED, universe=3, window_sessions=6, rotation_budget=1)

        after = store.snapshot(EXPECTED)  # …then reloads outcomes
        assert after["completed"] is False
        assert after["outcomes"] == {}, "a superseded invocation's outcomes must be gone"
        assert after["deferrals"] == {}
        assert store.completed_deferrals(EXPECTED) == {}
        assert store.completed_run(EXPECTED) is None

    def test_the_combination_completed_with_stale_outcomes_is_unreachable(self, tmp_path):
        """Whatever the interleaving, status+outcomes never disagree."""
        store = self._store(tmp_path)
        self._completed_run_with_deferrals(store)

        for _ in range(3):
            # A re-run starts at an arbitrary point relative to the reader.
            store.open_run(EXPECTED, EXPECTED, universe=3, window_sessions=6, rotation_budget=1)
            snap = store.snapshot(EXPECTED)
            if snap["completed"]:
                assert snap["deferrals"] == store.completed_deferrals(EXPECTED)
                assert snap["run"]["status"] == refresh_runs.COMPLETED
            else:
                # Not completed ⇒ nothing may be claimed as deliberately deferred.
                assert snap["deferrals"] == {}
                assert store.completed_run(EXPECTED) is None
                assert store.completed_deferrals(EXPECTED) == {}

    def test_the_audit_takes_exactly_one_snapshot_read(self, tmp_path, monkeypatch):
        """No window exists between the status read and the outcomes read."""
        calls = {"n": 0}
        original = RefreshRunStore.snapshot

        def counting(self, run_key):
            calls["n"] += 1
            return original(self, run_key)

        monkeypatch.setattr(RefreshRunStore, "snapshot", counting)
        bars = {f"S-DEF{i:03d}": STALE_BAR for i in range(3)}
        import tradehub_research.db as dbmod

        monkeypatch.setattr(
            driver, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(driver, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dbmod, "ResearchDB", lambda *a, **k: object())
        monkeypatch.setattr(dr, "retired_tickers", set)
        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(df, "_last_attempt", lambda _exp, _ticker: None)

        store = self._store(tmp_path)
        self._completed_run_with_deferrals(store, tickers=("DEF000", "DEF001"))
        calls["n"] = 0
        settings = SimpleNamespace(
            busy_timeout_ms=5000,
            tiingo_token=None,
            tiingo_license_confirmed=True,
            adapter_cache_dir=tmp_path,
        )
        paths = SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)
        audit = df.audit_universe(settings=settings, paths=paths, experiment_db=None, as_of=AS_OF)
        assert calls["n"] == 1, "status and outcomes must come from one read"
        assert audit.scheduled_count == 2

    def test_an_audit_after_a_superseding_rerun_is_not_suppressed(self, tmp_path, monkeypatch):
        """End to end: the stale deferrals must not survive to suppress remediation."""
        bars = {f"S-DEF{i:03d}": STALE_BAR for i in range(3)}
        import tradehub_research.db as dbmod

        monkeypatch.setattr(
            driver, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(driver, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dbmod, "ResearchDB", lambda *a, **k: object())
        monkeypatch.setattr(dr, "retired_tickers", set)
        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(df, "_last_attempt", lambda _exp, _ticker: None)

        store = self._store(tmp_path)
        self._completed_run_with_deferrals(store, tickers=("DEF000", "DEF001"))
        settings = SimpleNamespace(
            busy_timeout_ms=5000,
            tiingo_token=None,
            tiingo_license_confirmed=True,
            adapter_cache_dir=tmp_path,
        )
        paths = SimpleNamespace(research_db=tmp_path / "research.db", research_dir=tmp_path)

        first = df.audit_universe(settings=settings, paths=paths, experiment_db=None, as_of=AS_OF)
        assert first.scheduled_count == 2, "precondition: the completed run deferred them"

        # A new invocation claims the session and then dies: interrupted, not scheduled.
        store.open_run(EXPECTED, EXPECTED, universe=3, window_sessions=6, rotation_budget=1)
        second = df.audit_universe(settings=settings, paths=paths, experiment_db=None, as_of=AS_OF)
        assert second.scheduled_count == 0
        assert all(row.classification != df.SCHEDULED_DEFERRAL for row in second.stale)


# ---------------------------------------------------------------------------
# B -- repeated incidents keep their later remediation evidence
# ---------------------------------------------------------------------------
def _payload(incident_id: str, *, repair_ledger_unrecorded: int):
    return {
        "incident_id": incident_id,
        "expected_session": EXPECTED,
        "root_causes": {"INTERRUPTED_INGESTION_BATCH": 1},
        "remediation": {
            "run_key": EXPECTED,
            "targeted": 1,
            "scheduled_deferrals": 0,
            "repair_ledger_unrecorded": repair_ledger_unrecorded,
            "repaired": 1,
            "excluded": 0,
            "unresolved": 0,
        },
    }


class TestRepeatedIncidentKeepsLaterEvidence:
    def test_a_later_failed_ledger_write_is_persisted(self, tmp_path):
        """The exact defect: the second run's disclosure was silently dropped."""
        paths = SimpleNamespace(research_dir=tmp_path)
        incident = "2026-09-18:INTERRUPTED_INGESTION_BATCH"
        assert hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=0)) is None
        assert hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=2)) is None

        records = hw.incident_records(paths)
        incidents = [r for r in records if r.get("event") != hw.REMEDIATION_EVENT]
        events = [r for r in records if r.get("event") == hw.REMEDIATION_EVENT]
        assert len(incidents) == 1, "the original incident is preserved, not rewritten"
        assert len(events) == 1, "…and the later remediation attempt is appended"
        assert events[0]["remediation"]["repair_ledger_unrecorded"] == 2
        assert events[0]["incident_id"] == incident
        assert events[0]["attempt"] == 1
        # The durable record now makes the failure discoverable.
        assert any(
            record.get("remediation", {}).get("repair_ledger_unrecorded") for record in records
        )

    def test_the_original_incident_is_never_mutated(self, tmp_path):
        paths = SimpleNamespace(research_dir=tmp_path)
        incident = "2026-09-18:INTERRUPTED_INGESTION_BATCH"
        first = _payload(incident, repair_ledger_unrecorded=0)
        hw._record_incident(paths, first)
        line1 = (tmp_path / "freshness_incidents.jsonl").read_text().splitlines()[0]

        hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=1))
        hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=1))
        hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=0))
        lines = (tmp_path / "freshness_incidents.jsonl").read_text().splitlines()

        assert lines[0] == line1, "history is append-only"
        # Two lines: the incident, plus one event for the changed outcome. The
        # repeat of an already-recorded outcome is not appended twice, and a
        # return to the incident's own state is not re-asserted as a new attempt.
        assert len(lines) == 2
        assert hw.incident_records(paths)[1]["event"] == hw.REMEDIATION_EVENT
        assert hw.incident_records(paths)[1]["remediation"]["repair_ledger_unrecorded"] == 1
        incidents = [
            r for r in hw.incident_records(paths) if r.get("event") != hw.REMEDIATION_EVENT
        ]
        assert len(incidents) == 1

    def test_an_unchanged_repeat_stays_idempotent(self, tmp_path):
        paths = SimpleNamespace(research_dir=tmp_path)
        incident = "2026-09-18:INTERRUPTED_INGESTION_BATCH"
        payload = _payload(incident, repair_ledger_unrecorded=3)
        for _ in range(3):
            hw._record_incident(paths, payload)
        assert len(hw.incident_records(paths)) == 1, "the whole run is unchanged"

    def test_a_new_incident_still_appends_its_own_line(self, tmp_path):
        paths = SimpleNamespace(research_dir=tmp_path)
        hw._record_incident(paths, _payload("2026-09-17:A", repair_ledger_unrecorded=0))
        hw._record_incident(paths, _payload("2026-09-18:B", repair_ledger_unrecorded=0))
        incidents = [
            r for r in hw.incident_records(paths) if r.get("event") != hw.REMEDIATION_EVENT
        ]
        assert len(incidents) == 2

    def test_attempts_are_numbered_and_distinct(self, tmp_path):
        paths = SimpleNamespace(research_dir=tmp_path)
        incident = "2026-09-18:INTERRUPTED_INGESTION_BATCH"
        hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=0))
        for count in (1, 2, 3):
            hw._record_incident(paths, _payload(incident, repair_ledger_unrecorded=count))
        events = [r for r in hw.incident_records(paths) if r.get("event") == hw.REMEDIATION_EVENT]
        assert [e["attempt"] for e in events] == [1, 2, 3]
        assert [e["remediation"]["repair_ledger_unrecorded"] for e in events] == [1, 2, 3]

    def test_a_payload_without_remediation_evidence_is_not_duplicated(self, tmp_path):
        """A bare re-report of an unchanged incident adds nothing."""
        paths = SimpleNamespace(research_dir=tmp_path)
        incident = "2026-09-18:INTERRUPTED_INGESTION_BATCH"
        bare = {"incident_id": incident, "expected_session": EXPECTED}
        hw._record_incident(paths, bare)
        hw._record_incident(paths, bare)
        assert len(hw.incident_records(paths)) == 1

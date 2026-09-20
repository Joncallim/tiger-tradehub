"""Report accounting and rolling-symbol-capacity regression tests.

Two hardening concerns from the 2026-09-20 review:

1. The health report was semantically ambiguous -- "Fresh" appeared to mean
   "repaired this run" while "initially stale" meant "stale after the previous
   remediation", so the numbers could not be reconciled as a fleet snapshot.
   The report now names every count for exactly one thing and the arithmetic
   closes. These tests **re-parse the rendered report** and re-check the algebra,
   so a future edit that emits inconsistent counts fails here.

2. Tiingo's 450-distinct-symbol rolling-month ceiling was an operator note and
   the fleet sat at 444/450 -- six new symbols from discovering the ceiling by
   API failure. Capacity is now planned before spending, revisiting an
   already-reserved symbol is free, and overflow degrades deterministically.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradehub_research.ops import data_freshness as df
from tradehub_research.ops import downstream_guard as guard
from tradehub_research.ops.health_watch import (
    _reconcile_problems,
    reconciliation,
    render_freshness_report,
)
from tradehub_research.ops.symbol_capacity import (
    DEFAULT_SYMBOL_LIMIT,
    ROLLING_WINDOW_SECONDS,
    SymbolCapacityExceeded,
    capacity_report,
    plan_symbol_capacity,
    require_headroom,
)

NOW = 1_800_000_000.0  # fixed epoch: deterministic rolling-window arithmetic


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _stale(ticker: str, sid: str, last: str | None, cause: str = df.ROTATION_STARVED):
    return df.SecurityFreshness(
        ticker=ticker,
        security_id=sid,
        last_bar=last,
        missing_sessions=7,
        classification=cause,
    )


def _audit(*, fresh=0, within=0, stale=(), exceptions=()) -> df.AuditResult:
    result = df.AuditResult(
        expected_session="2026-09-18",
        universe=fresh + within + len(stale) + len(exceptions),
        fresh=fresh,
    )
    result.lagging_within_window = [f"W{i}" for i in range(within)]
    result.stale = list(stale)
    result.exceptions = [_stale(t, s, None, df.DELISTED_EMPTY) for t, s in exceptions]
    result.groups = {df.ROTATION_STARVED: [s.ticker for s in result.stale]} if result.stale else {}
    return result


def _summary(**over):
    base = {
        "repaired": 0,
        "excluded": 0,
        "unresolved": 0,
        "attempts": 0,
        "quota_blocked": False,
        "targeted": 0,
    }
    base.update(over)
    return base


def _settings(cache_dir=None):
    return SimpleNamespace(
        busy_timeout_ms=5000,
        tiingo_token=None,
        tiingo_license_confirmed=True,
        adapter_cache_dir=cache_dir or Path("/tmp"),
    )


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
        research_dir=tmp_path, research_db=db, experiment_db=tmp_path / "experiment.db"
    )


def _audit_simple(expected: str, stale_rows=(), exceptions=()) -> df.AuditResult:
    """Audit with an explicit expected session and (ticker, sid, last) stale rows."""
    result = df.AuditResult(
        expected_session=expected, universe=len(stale_rows) + len(exceptions), fresh=0
    )
    result.stale = [_stale(t, s, last) for t, s, last in stale_rows]
    result.exceptions = [_stale(t, s, None, df.DELISTED_EMPTY) for t, s in exceptions]
    return result


def _parse_report(lines: list[str]) -> dict[str, int]:
    """Pull the numbers back OUT of the rendered report.

    Deliberately parsing the text rather than reusing the code's dict: the point
    is to catch a renderer that prints numbers that do not reconcile.
    """
    text = "\n".join(lines)
    fields = {
        "universe_total": r"^Universe total:\s*([\d,]+)$",
        "excluded_exceptions": r"^Excluded legitimate exceptions:\s*([\d,]+)$",
        "eligible": r"^Eligible:\s*([\d,]+)$",
        "stale_before": r"^\s*stale before remediation:\s*([\d,]+)$",
        "repaired_this_run": r"^- repaired this run:\s*([\d,]+)$",
        "relieved_into_window": r"^- advanced into the rolling window:\s*([\d,]+)$",
        "newly_classified_exceptions": r"^- newly classified exceptions:\s*([\d,]+)$",
        "fresh_after": r"^Fresh after remediation:\s*([\d,]+)$",
        "stale_after": r"^Stale after remediation:\s*([\d,]+)$",
        "quarantined_after": r"^Quarantined after remediation:\s*([\d,]+)$",
    }
    out: dict[str, int] = {}
    for key, pattern in fields.items():
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            out[key] = int(match.group(1).replace(",", ""))
    fib = re.search(r"^\s*fresh before remediation:\s*([\d,]+)", text, re.MULTILINE)
    if fib:
        out["fresh_before"] = int(fib.group(1).replace(",", ""))
    return out


class _FakeQuota:
    """A rolling-month reservation set, backed by a dict -- no SQLite, no network."""

    def __init__(self, reserved: dict[str, float] | None = None, limit: int = DEFAULT_SYMBOL_LIMIT):
        self.reserved = dict(reserved or {})
        self.limit = limit
        self.reserve_calls: list[str] = []

    def _prune(self, now: float) -> None:
        for symbol, first in list(self.reserved.items()):
            if first <= now - ROLLING_WINDOW_SECONDS:
                del self.reserved[symbol]

    def bootstrap_usage(self, now: float, limit: int = DEFAULT_SYMBOL_LIMIT) -> dict:
        self._prune(now)
        symbols = [{"symbol": s, "first_requested_at": f} for s, f in sorted(self.reserved.items())]
        return {
            "used": len(symbols),
            "remaining": max(0, limit - len(symbols)),
            "limit": limit,
            "symbols": symbols,
        }

    def reserve_bootstrap_symbol(
        self, symbol: str, now: float, limit: int = DEFAULT_SYMBOL_LIMIT
    ) -> None:
        """Mirrors the real implementation, including when it raises."""
        self.reserve_calls.append(symbol.upper())
        self._prune(now)
        symbol = symbol.upper()
        if symbol in self.reserved:
            return
        if len(self.reserved) >= limit:
            raise RuntimeError("Tiingo 450-symbol rolling-month bootstrap ceiling reached")
        self.reserved[symbol] = now


def _reserved(count: int) -> dict[str, float]:
    return {f"S{i:03d}": NOW - 1000 for i in range(count)}


# ---------------------------------------------------------------------------
# 1. report accounting
# ---------------------------------------------------------------------------
class TestReconciledReport:
    def test_counts_reconcile_on_a_degraded_report(self):
        """The four invariants hold, and the quarantine matches the stale set."""
        stale = [_stale(f"T{i}", str(i), "2026-09-09") for i in range(278)]
        audit = _audit(fresh=45, within=80, stale=stale, exceptions=[("X1", "e1"), ("X2", "e2")])
        after = _audit(
            fresh=49, within=80, stale=stale[:274], exceptions=[("X1", "e1"), ("X2", "e2")]
        )
        lines = render_freshness_report(audit, after, _summary(repaired=4), {"active": 274})
        rec = _parse_report(lines)

        assert rec["universe_total"] == rec["eligible"] + rec["excluded_exceptions"]
        assert rec["eligible"] == rec["fresh_before"] + rec["stale_before"]
        assert rec["fresh_after"] == rec["fresh_before"] + rec["repaired_this_run"]
        assert rec["stale_after"] == (
            rec["stale_before"] - rec["repaired_this_run"] - rec["newly_classified_exceptions"]
        )
        assert rec["quarantined_after"] == rec["stale_after"]
        assert "REPORT INTEGRITY ERROR" not in "\n".join(lines)

    def test_repaired_is_never_labelled_fresh(self):
        """'Fresh' must mean 'has the expected session', not 'repaired this run'."""
        audit = _audit(
            fresh=10, stale=[_stale("A", "1", "2026-09-09"), _stale("B", "2", "2026-09-09")]
        )
        # A consistent after-state: one repair landed, one name still stale.
        after = _audit(fresh=11, stale=[_stale("B", "2", "2026-09-09")])
        lines = render_freshness_report(audit, after, _summary(repaired=1), {"active": 1})
        text = "\n".join(lines)
        assert "- repaired this run: 1" in text
        assert "Fresh after remediation: 11" in text
        # No bare "Fresh: N" line that could be read as the repair count, and no
        # line that ties "Fresh" to the repair figure.
        assert not re.search(r"^Fresh:\s*\d+$", text, re.MULTILINE)
        assert not re.search(r"Fresh[^\n]*repaired", text, re.IGNORECASE)
        assert "REPORT INTEGRITY ERROR" not in text

    def test_render_flags_inconsistent_counts_instead_of_hiding_them(self):
        """A structurally impossible audit must not render as a plausible report."""
        audit = _audit(fresh=10, stale=[_stale("A", "1", "2026-09-09")])
        audit.universe = audit.universe + 5  # break universe_total = eligible + exceptions
        lines = render_freshness_report(audit, audit, _summary(), {"active": 1})
        assert "REPORT INTEGRITY ERROR" in "\n".join(lines)

    def test_render_flags_a_quarantine_that_does_not_cover_the_stale_set(self):
        """Do not weaken the quarantine: fewer quarantined than stale is a defect."""
        stale = [_stale("A", "1", "2026-09-09"), _stale("B", "2", "2026-09-09")]
        audit = _audit(fresh=1, stale=stale)
        lines = render_freshness_report(audit, audit, _summary(), {"active": 1})
        text = "\n".join(lines)
        assert "REPORT INTEGRITY ERROR" in text
        assert "quarantine" in text

    def test_audit_invariants_are_checked_by_the_audit_itself(self):
        audit = _audit(fresh=3, within=2, stale=[_stale("A", "1", "2026-09-09")])
        assert audit.invariant_errors() == []
        audit.fresh = 999
        assert audit.invariant_errors() != []

    def test_auto_recovered_report_also_reconciles(self):
        stale = [_stale("A", "1", "2026-09-09")]
        audit = _audit(fresh=5, stale=stale)
        after = _audit(fresh=6, stale=[])
        lines = render_freshness_report(audit, after, _summary(repaired=1), {"active": 0})
        text = "\n".join(lines)
        assert text.startswith("TRADEHUB WATCH — AUTO-RECOVERED")
        assert "Universe total:" in text and "Eligible:" in text
        assert "Stale after remediation: 0" in text
        assert "Quarantined after remediation: 0" in text
        assert "REPORT INTEGRITY ERROR" not in text

    def test_reconciliation_is_pure_and_self_consistent(self):
        stale = [_stale(f"T{i}", str(i), "2026-09-09") for i in range(20)]
        audit = _audit(fresh=4, within=6, stale=stale, exceptions=[("X", "e")])
        after = _audit(fresh=9, within=6, stale=stale[:15], exceptions=[("X", "e")])
        rec = reconciliation(audit, after, _summary(repaired=5, excluded=0))
        assert rec["universe_total"] == 4 + 6 + 20 + 1
        assert rec["eligible"] == 4 + 6 + 20
        assert rec["fresh_before"] == 10
        assert rec["stale_after"] == 15
        assert rec["fresh_after"] == 15

    def test_newly_classified_exception_reduces_stale_without_counting_as_repaired(self):
        """A symbol that turns out to be delisted must leave the stale set without
        inflating the repaired count."""
        stale = [_stale("A", "1", "2026-09-09"), _stale("B", "2", "2026-09-09")]
        audit = _audit(fresh=0, stale=stale)
        after = _audit(fresh=0, stale=stale[:1])
        lines = render_freshness_report(
            audit, after, _summary(repaired=0, excluded=1), {"active": 1}
        )
        rec = _parse_report(lines)
        assert rec["stale_after"] == rec["stale_before"] - 0 - 1
        assert rec["fresh_after"] == rec["fresh_before"] + 0

    def test_quota_and_time_budget_pauses_are_disclosed(self):
        audit = _audit(fresh=1, stale=[_stale("A", "1", "2026-09-09")])
        lines = render_freshness_report(
            audit, audit, _summary(quota_blocked=True, time_budget_exhausted=True), {"active": 1}
        )
        text = "\n".join(lines)
        assert "paused on the provider quota reserve" in text
        assert "hit its time budget" in text

    def test_reconciliation_uses_the_reaudit_not_arithmetic_on_the_summary(self):
        """The after-counts must come from the re-audit.

        Regression for a real live defect: the run's `excluded` count was
        subtracted from the BEFORE audit, which had already moved those symbols
        out of its stale set -- double-counting, so the derived stale count (193)
        disagreed with the actual quarantine (233) and the report raised an
        integrity error.
        """
        stale = [_stale(f"T{i}", str(i), "2026-09-09") for i in range(10)]
        audit = _audit(fresh=5, stale=stale)
        # The re-audit says 9 are still stale -- trust it, do not recompute.
        after = _audit(fresh=6, stale=stale[:9])
        # 1 repair landed (at_expected 5 -> 6).
        rec = reconciliation(audit, after, _summary(repaired=1))
        assert rec["stale_after"] == 9  # from the re-audit
        assert rec["fresh_after"] == 6  # from the re-audit
        assert rec["repaired_this_run"] == 1  # observed from the audits
        assert rec["reported_repaired"] == 1
        # and the algebra still closes with all three transitions present
        assert rec["stale_after"] == (
            rec["stale_before"]
            - rec["repaired_this_run"]
            - rec["relieved_into_window"]
            - rec["newly_classified_exceptions"]
        )
        assert _reconcile_problems(rec) == []

    def test_a_symbol_advanced_into_the_rolling_window_is_accounted_for(self):
        """The transition that actually bit in the live run.

        A fetch can advance a symbol without reaching the expected session: it
        leaves the stale set (no longer materially stale) while never counting as
        a repair. Observed live as ``within_rolling_window`` 80 -> 81 alongside
        ``at_expected`` 90 -> 134, which made fresh_after 215 instead of 214 and
        tripped the integrity check. It must be an explicit field, not a drift.
        """
        stale = [_stale(f"T{i}", str(i), "2026-09-09") for i in range(45)]
        audit = _audit(fresh=90, within=80, stale=stale)
        after = _audit(fresh=134, within=81, stale=[])
        rec = reconciliation(audit, after, _summary(repaired=44, excluded=0))

        assert rec["repaired_this_run"] == 44  # at_expected 90 -> 134
        assert rec["relieved_into_window"] == 1  # within_window 80 -> 81
        assert rec["newly_classified_exceptions"] == 0
        assert rec["fresh_after"] == 215  # 134 + 81
        assert rec["fresh_before"] == 170  # 90 + 80
        assert rec["fresh_after"] == rec["fresh_before"] + 44 + 1
        assert rec["stale_after"] == 0  # from the re-audit
        # the three transitions fully account for the fall
        assert rec["stale_before"] - rec["stale_after"] == 44 + 1 + 0
        assert _reconcile_problems(rec) == []

    def test_a_disagreeing_repair_count_is_reported(self):
        """The run's claim must match the observed rise, or say so."""
        stale = [_stale(f"T{i}", str(i), "2026-09-09") for i in range(45)]
        audit = _audit(fresh=90, stale=stale)
        after = _audit(fresh=134, stale=stale[:1])  # 44 landed...
        rec = reconciliation(audit, after, _summary(repaired=45))  # ...but claimed 45
        problems = _reconcile_problems(rec)
        assert any("reported 45 repairs but the database shows 44" in p for p in problems)
        lines = render_freshness_report(audit, after, _summary(repaired=45), {"active": 1})
        assert "REPORT INTEGRITY ERROR" in "\n".join(lines)

    def test_quarantine_disagreement_is_always_reported(self):
        """If the guard covers fewer names than the audit calls stale, say so."""
        import tradehub_research.ops.health_watch as hw

        stale = [_stale(f"T{i}", str(i), "2026-09-09") for i in range(10)]
        audit = _audit(fresh=5, stale=stale)
        after = _audit(fresh=5, stale=stale)
        lines = hw.render_freshness_report(audit, after, _summary(), {"active": 7})
        text = "\n".join(lines)
        assert "REPORT INTEGRITY ERROR" in text
        assert "quarantined_after(7) != stale_after(10)" in text

    def test_empty_fetch_finding_is_written_to_the_append_only_ledger(self, tmp_path, monkeypatch):
        """(1) An EMPTY fetch must reach the ledger, not only the checkpoint.

        The audit classifies from `backfill_attempt`; if the delisting finding
        lives only in the checkpoint the symbol keeps reading as rotation-starved
        and the quarantine (audit-driven) diverges from the remediation.
        """
        recorded: list[dict] = []
        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: {"1": "2026-09-09"}.get(sid))
        import tradehub_research.backfill.tiingo_driver as drv

        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda experiment_db, *, ticker, status, http_status, bytes_count, error: (
                recorded.append({"ticker": ticker, "status": status, "error": error})
            ),
        )
        monkeypatch.setattr(drv, "classify_error", lambda exc: ("EMPTY", "0 bars", None))

        def returns_empty(ticker):
            raise RuntimeError("no bars for this symbol")

        store = df.CheckpointStore(tmp_path / "c.sqlite")
        audit = _audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")])
        summary = df.remediate(
            settings=_settings(),
            experiment_db=object(),
            paths=_paths(tmp_path),
            audit=audit,
            store=store,
            refresh_one=returns_empty,
        )
        assert summary["excluded"] == 1
        assert summary["repaired"] == 0
        assert len(recorded) == 1  # EXACTLY once
        assert recorded[0]["ticker"] == "AAA"
        assert recorded[0]["status"] == "ERROR"
        assert recorded[0]["error"].startswith("EMPTY")
        row = store.all_symbols(summary["run_key"])[0]
        assert row["disposition"] == "EXCLUDED"
        assert row["classification"] == df.DELISTED_EMPTY

    def test_remediation_is_idempotent_and_does_not_double_count_the_exception(
        self, tmp_path, monkeypatch
    ):
        """(5) Re-running must not write the exception finding twice."""
        recorded: list[dict] = []
        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: {"1": "2026-09-09"}.get(sid))
        import tradehub_research.backfill.tiingo_driver as drv

        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda experiment_db, *, ticker, status, http_status, bytes_count, error: (
                recorded.append({"ticker": ticker})
            ),
        )
        monkeypatch.setattr(drv, "classify_error", lambda exc: ("EMPTY", "0 bars", None))

        def returns_empty(ticker):
            raise RuntimeError("no bars")

        store = df.CheckpointStore(tmp_path / "c.sqlite")
        audit = _audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")])
        first = df.remediate(
            settings=_settings(),
            experiment_db=object(),
            paths=_paths(tmp_path),
            audit=audit,
            store=store,
            refresh_one=returns_empty,
        )
        second = df.remediate(
            settings=_settings(),
            experiment_db=object(),
            paths=_paths(tmp_path),
            audit=audit,
            store=store,
            refresh_one=returns_empty,
        )
        assert first["excluded"] == 1
        assert second["excluded"] == 0  # already settled; not re-attempted
        assert len(recorded) == 1  # ledger has exactly one finding
        assert len(store.all_symbols(first["run_key"])) == 1  # no duplicate rows
        # (4) the exception left the stale accounting exactly once
        assert second["repaired"] == 0

    def test_programmer_fault_in_the_ledger_write_propagates(self, tmp_path, monkeypatch):
        """(6) A missing dependency / NameError must NOT be swallowed.

        This is the exact defect: `record_attempt` was referenced without being
        imported, and a broad `except Exception` turned the NameError into a
        quietly-skipped ledger write.
        """
        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: {"1": "2026-09-09"}.get(sid))
        import tradehub_research.backfill.tiingo_driver as drv

        monkeypatch.setattr(drv, "classify_error", lambda exc: ("EMPTY", "0 bars", None))

        def returns_empty(ticker):
            raise RuntimeError("no bars")

        # (a) NameError -- the true defect
        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda *a, **k: (_ for _ in ()).throw(
                NameError("name 'record_attempt' is not defined")
            ),
        )
        with pytest.raises(NameError):
            df.remediate(
                settings=_settings(),
                experiment_db=object(),
                paths=_paths(tmp_path),
                audit=_audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")]),
                store=df.CheckpointStore(tmp_path / "a.sqlite"),
                refresh_one=returns_empty,
            )

        # (b) TypeError from a signature/dependency change
        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda *a, **k: (_ for _ in ()).throw(
                TypeError("record_attempt() got an unexpected kwarg")
            ),
        )
        with pytest.raises(TypeError):
            df.remediate(
                settings=_settings(),
                experiment_db=object(),
                paths=_paths(tmp_path),
                audit=_audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")]),
                store=df.CheckpointStore(tmp_path / "b.sqlite"),
                refresh_one=returns_empty,
            )

        # (c) a missing dependency
        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda *a, **k: (_ for _ in ()).throw(ImportError("no module named 'sqlite3'")),
        )
        with pytest.raises(ImportError):
            df.remediate(
                settings=_settings(),
                experiment_db=object(),
                paths=_paths(tmp_path),
                audit=_audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")]),
                store=df.CheckpointStore(tmp_path / "c.sqlite"),
                refresh_one=returns_empty,
            )

    def test_ledger_io_failure_fails_closed(self, tmp_path, monkeypatch):
        """(7) An EXPECTED ledger I/O failure has an explicit disposition: stop.

        The report derives its accounting from the audit, and the audit reads the
        ledger. A write that cannot land leaves the two disagreeing, so the run
        fails closed rather than emitting a reconciliation it cannot prove.
        """
        import sqlite3

        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: {"1": "2026-09-09"}.get(sid))
        import tradehub_research.backfill.tiingo_driver as drv

        monkeypatch.setattr(drv, "classify_error", lambda exc: ("EMPTY", "0 bars", None))
        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")),
        )

        def returns_empty(ticker):
            raise RuntimeError("no bars")

        with pytest.raises(df.LedgerPersistenceError) as excinfo:
            df.remediate(
                settings=_settings(),
                experiment_db=object(),
                paths=_paths(tmp_path),
                audit=_audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")]),
                store=df.CheckpointStore(tmp_path / "c.sqlite"),
                refresh_one=returns_empty,
            )
        assert "disk I/O error" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)

    def test_ledger_io_failure_does_not_produce_a_successful_report(self, tmp_path, monkeypatch):
        """(7, continued) The watch must not report success when evidence failed."""
        from tradehub_research.ops import health_watch

        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: {"1": "2026-09-09"}.get(sid))
        import tradehub_research.backfill.tiingo_driver as drv

        monkeypatch.setattr(drv, "classify_error", lambda exc: ("EMPTY", "0 bars", None))
        monkeypatch.setattr(
            drv,
            "record_attempt",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only file system")),
        )

        def returns_empty(ticker):
            raise RuntimeError("no bars")

        audit = _audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")])
        monkeypatch.setattr(df, "audit_universe", lambda **kw: audit)
        # Drive the real remediate(), but with the refresh hook injected so no
        # provider adapter is needed (no token in tests).
        real_remediate = df.remediate
        monkeypatch.setattr(
            df,
            "remediate",
            lambda **kw: real_remediate(refresh_one=returns_empty, **kw),
        )
        paths = _paths(tmp_path)
        monkeypatch.setattr(health_watch, "ALERTS", [])
        with pytest.raises(df.LedgerPersistenceError):
            health_watch.check_data_freshness(_settings(cache_dir=tmp_path), paths)
        # Nothing was emitted: no misleading "healthy"/"degraded" report.
        assert health_watch.ALERTS == []

    def test_audit_derives_the_exception_classification_from_ledger_state(
        self, tmp_path, monkeypatch
    ):
        """(2) The audit must reach the same conclusion from persisted state.

        Writes a real EMPTY row into a real append-only ledger, then asserts the
        classification the audit derives from it -- closing the loop between what
        remediation records and what the next audit reports.
        """
        from tradehub_research.db import ResearchDB
        from tradehub_research.evidence import EvidenceStore
        from tradehub_research.validation.experiment_db import ExperimentDB

        db = ResearchDB(tmp_path / "research.db", 5000)
        db.migrate()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
                ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
            )
            conn.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "S1",
                    "DEAD",
                    "US",
                    "Dead Co",
                    "Tech",
                    "Software",
                    "SUPPORTED",
                    "2026-01-01T00:00:00Z",
                    None,
                ),
            )
        store = EvidenceStore(db)
        store.insert(
            security_id="S1",
            source_id="tiingo_eod",
            structured_fields={"record_type": "price_bar", "session_date": "2026-09-09"},
            extraction_confidence=1.0,
            event_time="2026-09-09",
            public_available_time="2026-09-10T00:15:00Z",
            pat_provenance="derived_from_index",
            ingested_time="2026-09-11T02:00:00Z",
            source_record_id="tiingo:DEAD:2026-09-09",
        )

        exp = ExperimentDB(tmp_path / "experiment.db")
        exp.migrate()
        df._ledger_write(exp, ticker="DEAD", error="EMPTY: 0 bars parsed (delisted/unresolvable)")

        attempt = df._last_attempt(exp, "DEAD")
        assert attempt is not None
        assert df._classify_from_attempt(attempt["error"], attempt["status"]) == df.DELISTED_EMPTY
        assert df.DELISTED_EMPTY in df.LEGITIMATE_EXCEPTIONS

    def test_quota_introspection_boundary(self, tmp_path, monkeypatch):
        """Programmer faults in the quota probe propagate; I/O failures do not."""
        import sqlite3

        class _Quota:
            def __init__(self, exc):
                self.exc = exc

            def remaining(self, now):
                if self.exc:
                    raise self.exc
                return {"hourly": 5, "daily": 100}

        def _adapter(exc):
            return SimpleNamespace(quota=_Quota(exc))

        # readable -> exact value
        assert df._quota_hourly_remaining(_adapter(None)) == 5
        # exhausted -> 0, so the caller pauses instead of sleeping in the window
        assert df._quota_hourly_remaining(_adapter(None)) is not None
        # expected I/O failure -> None (proceed and let the request decide),
        # never a fabricated "exhausted" verdict that would skip the queue
        assert (
            df._quota_hourly_remaining(
                _adapter(sqlite3.OperationalError("unable to open database file"))
            )
            is None
        )
        assert df._quota_hourly_remaining(_adapter(OSError("read-only file system"))) is None
        # programmer faults propagate
        with pytest.raises(TypeError):
            df._quota_hourly_remaining(
                _adapter(TypeError("remaining() takes 1 positional argument"))
            )
        with pytest.raises(AttributeError):
            df._quota_hourly_remaining(SimpleNamespace(quota=object()))

    def test_exhausted_quota_pauses_the_run_without_recording_an_attempt(
        self, tmp_path, monkeypatch
    ):
        """An exhausted pre-flight budget stops the run and touches no symbol."""
        monkeypatch.setattr(df, "_last_bar", lambda _db, sid: "2026-09-09")

        class _Quota:
            def remaining(self, now):
                return {"hourly": 0, "daily": 100}

            def bootstrap_usage(self, now, limit=450):
                return {"used": 1, "remaining": limit - 1, "limit": limit, "symbols": []}

        calls: list[str] = []
        store = df.CheckpointStore(tmp_path / "c.sqlite")
        summary = df.remediate(
            settings=_settings(),
            experiment_db=None,
            paths=_paths(tmp_path),
            audit=_audit_simple("2026-09-18", [("AAA", "1", "2026-09-09")]),
            store=store,
            adapter=SimpleNamespace(quota=_Quota()),
        )
        assert summary["quota_blocked"] is True
        assert summary["attempts"] == 0  # the provider was never called
        assert calls == []
        row = store.all_symbols(summary["run_key"])[0]
        assert row["disposition"] == "PENDING"
        assert int(row["attempts"]) == 0
        assert row["last_attempt_at"] is None

    def test_symbol_capacity_state_boundary(self, tmp_path, monkeypatch):
        """Reporting must not swallow a programming fault into an empty block."""
        import sqlite3

        from tradehub_research.ops import health_watch

        class _Boom:
            def __init__(self, exc):
                self.exc = exc

            def remaining(self, now):
                return {"hourly": 1, "daily": 1}

            def bootstrap_usage(self, now, limit=450):
                raise self.exc

        import tradehub_research.adapters.tiingo as tiingo_mod

        # (a) expected I/O -> reported as an explicit degradation, not silence
        monkeypatch.setattr(
            tiingo_mod,
            "TiingoQuota",
            lambda **kw: _Boom(sqlite3.OperationalError("database is locked")),
        )
        state = health_watch._symbol_capacity_state(_settings(cache_dir=tmp_path))
        assert "error" in state and "database is locked" in state["error"]

        # (b) programmer fault -> propagates
        monkeypatch.setattr(
            tiingo_mod,
            "TiingoQuota",
            lambda **kw: _Boom(AttributeError("no attribute 'bootstrap_usage'")),
        )
        with pytest.raises(AttributeError):
            health_watch._symbol_capacity_state(_settings(cache_dir=tmp_path))

    def test_quarantine_removes_exceptions_exactly_once(self, tmp_path):
        """(3,4) quarantined_after == stale_after, and exceptions leave once."""
        stale = [
            {"security_id": str(i), "ticker": f"T{i}", "classification": df.ROTATION_STARVED}
            for i in range(10)
        ]
        guard.sync_quarantine(stale, expected_session="2026-09-18", research_dir=tmp_path)
        assert len(guard.stale_security_ids(tmp_path)) == 10

        # Three turn out to be legitimate exceptions; the audit now reports 7 stale.
        remaining = stale[:7]
        result = guard.sync_quarantine(
            remaining, expected_session="2026-09-18", research_dir=tmp_path
        )
        assert result["active"] == 7
        assert len(result["cleared"]) == 3
        assert len(guard.stale_security_ids(tmp_path)) == 7

        # Re-running with the same stale set changes nothing (no double-removal).
        again = guard.sync_quarantine(
            remaining, expected_session="2026-09-18", research_dir=tmp_path
        )
        assert again["active"] == 7
        assert again["cleared"] == []
        assert again["quarantined"] == []

    def test_symbol_capacity_is_surfaced_in_the_report(self):
        """The licence ceiling must be visible before it binds, not after."""
        audit = _audit(fresh=1, stale=[_stale("A", "1", "2026-09-09")])
        lines = render_freshness_report(
            audit,
            audit,
            _summary(),
            {"active": 1},
            {"used": 444, "limit": 450, "headroom": 6, "deferred": 0, "at_capacity": False},
        )
        text = "\n".join(lines)
        assert "Rolling-month symbol capacity:" in text
        assert "444/450 distinct symbols reserved" in text
        assert "headroom 6" in text
        assert "AT CAPACITY" not in text

    def test_capacity_exhaustion_is_called_out(self):
        audit = _audit(fresh=1, stale=[_stale("A", "1", "2026-09-09")])
        lines = render_freshness_report(
            audit,
            audit,
            _summary(),
            {"active": 1},
            {"used": 450, "limit": 450, "headroom": 0, "deferred": 3, "at_capacity": True},
        )
        text = "\n".join(lines)
        assert "AT CAPACITY" in text
        assert "3 new symbols could not be admitted" in text


# ---------------------------------------------------------------------------
# 2. rolling-month symbol capacity
# ---------------------------------------------------------------------------
class TestSymbolCapacity:
    @pytest.mark.parametrize("used,expected_headroom", [(448, 2), (449, 1), (450, 0), (451, 0)])
    def test_headroom_at_the_boundary(self, used, expected_headroom):
        """448/449/450/451 distinct symbols behave deterministically."""
        quota = _FakeQuota(_reserved(used), limit=450)
        plan = plan_symbol_capacity(quota, ["NEW1", "NEW2", "NEW3"], now=NOW)
        assert plan.used == min(used, 450) or used == 451
        assert plan.headroom == expected_headroom
        assert len(plan.admissible_new) == expected_headroom
        assert len(plan.deferred) == 3 - expected_headroom

    def test_planning_is_read_only(self):
        """A plan must never spend a reservation or quota."""
        quota = _FakeQuota(_reserved(440), limit=450)
        plan_symbol_capacity(quota, ["NEW1"], now=NOW)
        assert quota.reserve_calls == []
        assert len(quota.reserved) == 440

    def test_revisiting_a_reserved_symbol_is_free_at_the_ceiling(self):
        """At 450/450 the fleet must still refresh symbols it already owns."""
        quota = _FakeQuota(_reserved(450), limit=450)
        plan = plan_symbol_capacity(quota, ["S000", "S001"], now=NOW)
        assert plan.already_reserved == ["S000", "S001"]
        assert plan.deferred == []
        assert plan.headroom == 0
        assert plan.at_capacity is True
        # And the reservation path agrees: no raise for an existing symbol.
        require_headroom(quota, "S000", now=NOW)

    def test_new_symbol_at_the_ceiling_is_deferred_not_fetched(self):
        quota = _FakeQuota(_reserved(450), limit=450)
        plan = plan_symbol_capacity(quota, ["BRANDNEW"], now=NOW)
        assert plan.admissible_new == []
        assert plan.deferred == ["BRANDNEW"]
        assert "capacity full (450/450)" in plan.reasons["BRANDNEW"]
        with pytest.raises(SymbolCapacityExceeded) as excinfo:
            require_headroom(quota, "BRANDNEW", now=NOW)
        assert excinfo.value.used == 450 and excinfo.value.limit == 450

    def test_451_still_reports_zero_headroom_and_no_negative(self):
        """A set somehow over the limit must clamp, never go negative."""
        quota = _FakeQuota(_reserved(451), limit=450)
        plan = plan_symbol_capacity(quota, ["NEW"], now=NOW)
        assert plan.headroom == 0
        assert plan.used == 451
        assert plan.deferred == ["NEW"]

    def test_capacity_overflow_degrades_deterministically(self):
        """Same input -> same admitted/deferred split, and it is reported."""
        quota = _FakeQuota(_reserved(448), limit=450)
        requested = ["N1", "N2", "N3", "N4", "S000"]
        first = plan_symbol_capacity(quota, requested, now=NOW)
        second = plan_symbol_capacity(quota, requested, now=NOW)
        assert first.admissible_new == second.admissible_new == ["N1", "N2"]
        assert first.deferred == second.deferred == ["N3", "N4"]
        assert first.already_reserved == ["S000"]
        assert quota.reserve_calls == []  # no churn from planning

    def test_universe_churn_keeps_reserved_symbols_admissible(self):
        """Churn: brand-new names must not evict the ability to refresh the fleet."""
        quota = _FakeQuota(_reserved(449), limit=450)
        # 1 slot free: the active name wins it, the new name is deferred.
        plan = plan_symbol_capacity(quota, ["NEWNAME", "S100"], now=NOW, active=["S100", "NEWNAME"])
        assert plan.admissible_new == ["NEWNAME"]
        assert plan.already_reserved == ["S100"]
        assert plan.deferred == []
        # Now saturate: the fleet's own symbols stay refreshable, new ones queue.
        quota = _FakeQuota(_reserved(450), limit=450)
        plan = plan_symbol_capacity(quota, ["NEW1", "S200"], now=NOW, active=["S200"])
        assert plan.already_reserved == ["S200"]
        assert plan.deferred == ["NEW1"]

    def test_active_symbols_are_prioritised_for_the_last_slot(self):
        quota = _FakeQuota(_reserved(449), limit=450)
        plan = plan_symbol_capacity(quota, ["ZNEW", "ANEW"], now=NOW, active=["ANEW"])
        assert plan.admissible_new == ["ANEW"]  # active set first
        assert plan.deferred == ["ZNEW"]

    def test_next_headroom_is_reported_for_waiting_symbols(self):
        """Deferred symbols need to know when capacity frees, not just that it is full."""
        quota = _FakeQuota({"OLDEST": NOW - ROLLING_WINDOW_SECONDS + 3600}, limit=450)
        plan = plan_symbol_capacity(quota, ["NEW"], now=NOW)
        assert plan.next_headroom_at == pytest.approx(NOW + 3600)

    def test_expired_reservations_free_capacity(self):
        """The rolling window is real: a 31-day-old reservation no longer counts.

        An expired symbol is therefore a *new* symbol again -- re-admitting it
        consumes a fresh slot, which is exactly what the provider charges.
        """
        quota = _FakeQuota({"ANCIENT": NOW - ROLLING_WINDOW_SECONDS - 1}, limit=450)
        plan = plan_symbol_capacity(quota, ["ANCIENT", "NEW"], now=NOW)
        assert plan.used == 0
        assert plan.already_reserved == []
        assert plan.admissible_new == ["ANCIENT", "NEW"]

    def test_capacity_report_shape(self):
        quota = _FakeQuota(_reserved(444), limit=450)
        report = capacity_report(quota, now=NOW)
        assert report["used"] == 444
        assert report["limit"] == 450
        assert report["headroom"] == 6
        assert report["at_capacity"] is False

    def test_capacity_error_is_classified_as_capacity_not_quota(self):
        """A licence ceiling must not masquerade as a spent request budget."""
        from tradehub_research.backfill.tiingo_driver import classify_error

        cls, detail, http = classify_error(SymbolCapacityExceeded("NEW", 450, 450))
        assert cls == "CAPACITY"
        assert "450/450" in detail
        assert http is None
        # The legacy provider-side message is still recognised as capacity.
        cls, _, _ = classify_error(
            RuntimeError("Tiingo 450-symbol rolling-month bootstrap ceiling reached")
        )
        assert cls == "CAPACITY"
        # ...while a genuine request-budget stop remains QUOTA.
        cls, _, _ = classify_error(
            RuntimeError("Tiingo quota reserve reached; ingestion failed closed")
        )
        assert cls == "QUOTA"


class TestQuarantineUnaffectedByAccounting:
    """The accounting pass must not weaken downstream protection."""

    def test_stale_set_is_fully_quarantined(self, tmp_path):
        stale = [
            {"security_id": str(i), "ticker": f"T{i}", "classification": df.ROTATION_STARVED}
            for i in range(25)
        ]
        result = guard.sync_quarantine(stale, expected_session="2026-09-18", research_dir=tmp_path)
        assert result["active"] == 25
        assert guard.stale_security_ids(tmp_path) == frozenset(str(i) for i in range(25))

    def test_no_bars_for_quarantined_and_bars_return_after_clear(self, tmp_path, monkeypatch):
        from tradehub_research.hunters import common as hc

        # `hunters.common.data_stale` resolves the guard from the runtime research
        # dir, exactly as the deployed hunters do.
        monkeypatch.setenv("TRADEHUB_RESEARCH_DIR", str(tmp_path))
        guard.mark_data_stale("42", ticker="T42", research_dir=tmp_path)
        assert hc.data_stale("42") is True
        guard.clear_data_stale("42", research_dir=tmp_path)
        assert hc.data_stale("42") is False

    def test_reclassification_refreshes_the_reason_without_duplicating(self, tmp_path):
        """The quarantine record must not keep a superseded reason string.

        Regression for a real inconsistency: METRY stayed recorded as
        FETCHED_NOT_STORED after the audit had correctly reclassified it as an
        unpublished-session timing fault, so the evidence contradicted the report.
        """
        guard.mark_data_stale(
            "1",
            ticker="METRY",
            expected_session="2026-09-18",
            reason=df.VALIDATION_FAILURE,
            research_dir=tmp_path,
            now=datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc),
        )
        first = json.loads(guard.quarantine_path(tmp_path).read_text())[0]
        assert first["reason"] == df.VALIDATION_FAILURE
        assert first["classified_at"] == "2026-09-20T01:00:00+00:00"

        # Same symbol, corrected classification -> reason refreshed, not duplicated.
        changed = guard.mark_data_stale(
            "1",
            ticker="METRY",
            expected_session="2026-09-18",
            reason=df.NOT_YET_PUBLISHED,
            research_dir=tmp_path,
            now=datetime(2026, 9, 20, 2, 0, tzinfo=timezone.utc),
        )
        assert changed is False  # still reported as "not newly marked"
        items = json.loads(guard.quarantine_path(tmp_path).read_text())
        assert len(items) == 1  # no duplicate record
        assert items[0]["reason"] == df.NOT_YET_PUBLISHED  # audit trail corrected
        assert items[0]["classified_at"] == "2026-09-20T01:00:00+00:00"  # first seen preserved
        assert items[0]["reason_updated_at"] == "2026-09-20T02:00:00+00:00"
        assert guard.stale_reason("1", tmp_path) == df.NOT_YET_PUBLISHED
        assert len(guard.stale_security_ids(tmp_path)) == 1  # count unchanged


class TestDeployedWatchScriptMatchesSource:
    """The server must not drift from Git.

    The health-watch script is installed outside the TradeHub systemd estate, so
    `deploy/hermes/tradehub-health-watch.sh` is its canonical source. This guard
    fails if the host copy has been edited directly.
    """

    INSTALLED = "/var/lib/hermes/scripts/tradehub-health-watch.sh"
    SOURCE = "deploy/hermes/tradehub-health-watch.sh"

    def test_installed_watch_script_equals_repo_source(self):
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        source = repo_root / self.SOURCE
        installed = Path(self.INSTALLED)
        assert source.exists(), f"canonical source missing: {self.SOURCE}"
        if not installed.exists():
            pytest.skip(f"{self.INSTALLED} not present on this host (e.g. CI)")
        assert installed.read_bytes() == source.read_bytes(), (
            "the deployed health-watch script differs from "
            f"{self.SOURCE}; reinstall it from the repo instead of editing the host copy"
        )


class TestUnpublishedSessionReproduction:
    """METRY (0002073643) end-to-end: why a successful fetch persisted nothing.

    Provider truth (proved from METRY's own stored rows): Tiingo's EOD
    ``public_available_time`` for session D is ``D+1T00:15:00Z`` -- the same
    instant as the repo's 20:15 ET bar-eligible boundary. So a bar for session D
    cannot legally be ingested before 00:15Z the following UTC day.

    METRY's ledger row: 2026-09-10T04:26:37Z, status ERROR,
    ``PARSE: public_available_time cannot follow ingested_time``, http_status NULL.
    Its stored rows stop at session 2026-09-09 with ingested_time
    2026-09-10T04:26:36Z -- one second earlier. So a request range that reached
    into the *current* (not yet published) session had its earlier bars stored and
    then raised on the current-session bar, failing the whole attempt.
    """

    SID = "0002073643"
    TICKER = "METRY"
    INGESTED = "2026-09-10T04:26:37Z"

    def _seed(self, tmp_path):
        from tradehub_research.db import ResearchDB
        from tradehub_research.evidence import EvidenceStore

        db = ResearchDB(tmp_path / "research.db", 5000)
        db.migrate()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
                ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
            )
            conn.execute(
                "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.SID,
                    self.TICKER,
                    "US",
                    "Metri Inc",
                    "Technology",
                    "Software",
                    "SUPPORTED",
                    "2026-01-01T00:00:00Z",
                    None,
                ),
            )
        return db, EvidenceStore(db)

    def _bar(self, store, session: str, pat: str, ingested: str):
        return store.insert(
            security_id=self.SID,
            source_id="tiingo_eod",
            structured_fields={
                "record_type": "price_bar",
                "provider_ticker": self.TICKER,
                "session_date": session,
                "close": 1.0,
            },
            extraction_confidence=1.0,
            event_time=session,
            public_available_time=pat,
            pat_provenance="derived_from_index",
            ingested_time=ingested,
            source_record_id=f"tiingo:{self.TICKER}:{session}",
        )

    def _last_bar(self, db):
        with db.connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT MAX(json_extract(structured_fields,'$.session_date')) AS d "
                "FROM evidence_event WHERE security_id=? AND source_id='tiingo_eod'",
                (self.SID,),
            ).fetchone()
        return row["d"] if row else None

    def test_unpublished_session_bar_is_rejected_and_nothing_is_persisted(self, tmp_path):
        """The precise METRY failure, reproduced deterministically."""
        db, store = self._seed(tmp_path)
        # A completed session ingests cleanly (PAT already published).
        self._bar(store, "2026-09-09", "2026-09-10T00:15:00Z", self.INGESTED)
        assert self._last_bar(db) == "2026-09-09"

        # The current session's bar: fetched fine, refused by the evidence layer.
        with pytest.raises(ValueError, match="cannot follow ingested_time"):
            self._bar(store, "2026-09-10", "2026-09-11T00:15:00Z", self.INGESTED)

        # Nothing was persisted for the refused session, and the last good bar is
        # untouched -- exactly the state METRY is in.
        with db.connect(read_only=True) as conn:
            refused = conn.execute(
                "SELECT COUNT(*) FROM evidence_event WHERE security_id=? "
                "AND json_extract(structured_fields,'$.session_date')='2026-09-10'",
                (self.SID,),
            ).fetchone()[0]
        assert refused == 0
        assert self._last_bar(db) == "2026-09-09"

    def test_later_publication_time_is_still_legal(self, tmp_path):
        """A session whose PAT has passed ingests fine -- the rule is the PAT, not the date."""
        db, store = self._seed(tmp_path)
        self._bar(store, "2026-09-09", "2026-09-10T00:15:00Z", self.INGESTED)
        # Same session, ingested after its PAT: legal.
        self._bar(store, "2026-09-10", "2026-09-11T00:15:00Z", "2026-09-11T02:00:00Z")
        assert self._last_bar(db) == "2026-09-10"

    def test_refused_session_is_classified_as_timing_not_a_storage_defect(self):
        """METRY must not read as 'fetched but not stored' -- that blames storage."""
        from tradehub_research.backfill.tiingo_driver import classify_error

        exc = ValueError("public_available_time cannot follow ingested_time")
        assert classify_error(exc)[0] == "NOT_YET_PUBLISHED"
        assert df._DEFAULT_CLASS_BY_ERROR["NOT_YET_PUBLISHED"] == df.NOT_YET_PUBLISHED
        assert df._DEFAULT_CLASS_BY_ERROR["NOT_YET_PUBLISHED"] != df.VALIDATION_FAILURE
        assert df.NOT_YET_PUBLISHED in df.REMEDIABLE
        assert df.NOT_YET_PUBLISHED not in df.LEGITIMATE_EXCEPTIONS

    def test_historical_ledger_row_is_reclassified_from_its_stored_text(self):
        """`backfill_attempt` is append-only: METRY's row keeps the old `PARSE:` prefix.

        The classifier must therefore derive NOT_YET_PUBLISHED from the stored
        message text, or the live incident keeps reading as a storage defect.
        """
        # The exact string in the live ledger for METRY (0002073643).
        stored = "PARSE: public_available_time cannot follow ingested_time"
        assert df._classify_from_attempt(stored, "ERROR") == df.NOT_YET_PUBLISHED
        assert df._classify_from_attempt(stored, "ERROR") != df.VALIDATION_FAILURE
        # A genuine parse defect still classifies as a validation failure.
        assert (
            df._classify_from_attempt("PARSE: unexpected field", "ERROR") == df.VALIDATION_FAILURE
        )
        # And a pristine NEW-prefix row classifies identically.
        assert (
            df._classify_from_attempt(
                "NOT_YET_PUBLISHED: public_available_time cannot follow ingested_time", "ERROR"
            )
            == df.NOT_YET_PUBLISHED
        )

    def test_publication_aware_range_never_asks_for_an_unpublished_session(self):
        """The request-range invariant that makes the failure impossible.

        `expected_latest_session(now)` must only ever return a session whose EOD
        PAT (session+1 00:15Z) has already passed -- otherwise the caller is
        requesting a bar that cannot legally exist yet.
        """
        from datetime import datetime, timedelta, timezone

        from tradehub_research.ops.market_calendar import expected_latest_session

        for hours in range(0, 24 * 5):
            now = datetime(2026, 9, 14, tzinfo=timezone.utc) + timedelta(hours=hours)
            session = expected_latest_session(now)
            pat = datetime.combine(
                session + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ) + timedelta(minutes=15)
            assert pat <= now, f"session {session} PAT {pat} is unpublished at {now}"
            assert session <= now.date()

    def test_requesting_today_before_publication_is_the_defect(self):
        """Positive control: the old `end_date=today` bound violates the invariant."""
        from datetime import datetime, timezone

        from tradehub_research.ops.market_calendar import expected_latest_session

        # 00:26 ET on 2026-09-10, i.e. METRY's failure window.
        now = datetime(2026, 9, 10, 4, 26, tzinfo=timezone.utc)
        today = now.date()  # what the old bound used
        assert today == date(2026, 9, 10)
        assert expected_latest_session(now) == date(2026, 9, 9)  # publication-aware
        # The current session's PAT is in the future -> requesting it must fail.
        pat = datetime(2026, 9, 11, 0, 15, tzinfo=timezone.utc)
        assert pat > now

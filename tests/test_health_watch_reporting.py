"""Offline tests for the freshness report wording (reporting only, synthetic fixtures)."""

from __future__ import annotations

from types import SimpleNamespace

from tradehub_research.ops import health_watch as hw


def _audit(**kw):
    base = dict(
        expected_session="2026-10-23",
        universe_total=443,
        eligible=399,
        excluded_exceptions=44,
        fresh=45,
        fresh_before=364,
        stale_before=35,
        stale_count=35,
        stale=["STALE-1"],
        lagging_within_window=["L"] * 319,
    )
    base.update(kw)
    base.setdefault("invariant_errors", lambda: [])
    return SimpleNamespace(**base)


def _after(**kw):
    base = dict(
        expected_session="2026-10-23",
        universe_total=443,
        eligible=399,
        excluded_exceptions=44,
        fresh=80,
        fresh_before=399,
        stale_before=0,
        stale_count=0,
        stale=[],
        lagging_within_window=["L"] * 319,
    )
    base.update(kw)
    base.setdefault("invariant_errors", lambda: [])
    return SimpleNamespace(**base)


def test_recovered_report_splits_exact_session_from_window_only():
    lines = hw.render_freshness_report(_audit(), _after(), {"repaired": 35}, {"active": 0})
    text = "\n".join(lines)
    assert lines[0] == "TRADEHUB WATCH — FRESHNESS WINDOW RECOVERED"
    assert "  at expected session (2026-10-23): 80" in text
    assert "  within rolling window only: 319" in text
    assert "Fresh after remediation: 399" in text


def test_recovered_report_no_longer_claims_downstream_health():
    lines = hw.render_freshness_report(_audit(), _after(), {"repaired": 35}, {"active": 0})
    text = "\n".join(lines).lower()
    assert "healthy" not in text
    assert "not evaluated" in text
    assert "scoring" in text and "maturation" in text


def test_integrity_error_suppresses_recovery_headline():
    bad_audit = _audit(eligible=398)  # 443 != 398 + 44
    lines = hw.render_freshness_report(bad_audit, _after(), {"repaired": 35}, {"active": 0})
    text = "\n".join(lines)
    assert "RECOVERED" not in text
    assert "INTEGRITY ERROR" in lines[0]
    assert "Recovery not claimed" in text
    assert "universe_total != eligible + excluded_exceptions" in text


def test_reported_repaired_mismatch_suppresses_recovery_headline():
    lines = hw.render_freshness_report(_audit(), _after(), {"repaired": 80}, {"active": 0})
    text = "\n".join(lines)
    assert "RECOVERED" not in text
    assert "the run reported 80 repairs" in text


def test_no_incident_stays_silent():
    assert (
        hw.render_freshness_report(
            _audit(stale=[], stale_before=0, stale_count=0), _after(), {}, {"active": 0}
        )
        == []
    )


def test_integrity_path_preserves_operational_warnings():
    summary = {
        "repaired": 35,
        "quota_blocked": True,
        "time_budget_exhausted": True,
        "repair_ledger_unrecorded": 4,
    }
    capacity = {"used": 410, "limit": 450, "headroom": 40, "at_capacity": True, "deferred": 6}
    lines = hw.render_freshness_report(
        _audit(eligible=398), _after(), summary, {"active": 0}, capacity
    )
    text = "\n".join(lines)
    assert "INTEGRITY ERROR" in lines[0]
    assert "Operational warnings still in force:" in text
    assert "4 repair(s) could not be recorded" in text
    assert "provider quota reserve" in text
    assert "time budget" in text
    assert "410/450 distinct symbols reserved" in text
    assert "6 new symbols could not be admitted" in text


def test_after_audit_invariant_errors_are_checked():
    bad_after = _after()
    bad_after.invariant_errors = lambda: ["after audit: stale set disagrees with count"]
    lines = hw.render_freshness_report(_audit(), bad_after, {"repaired": 35}, {"active": 0})
    text = "\n".join(lines)
    assert "RECOVERED" not in text
    assert "after audit: stale set disagrees with count" in text


def test_nonzero_residual_blocks_recovery():
    lines = hw.render_freshness_report(_audit(), _after(), {"repaired": 35}, {"active": 3})
    text = "\n".join(lines)
    assert "RECOVERED" not in text
    assert "quarantined_after(3) != stale_after(0)" in text


def test_missing_residual_is_reported_unverified_not_zero():
    lines = hw.render_freshness_report(_audit(), _after(), {"repaired": 35}, {})
    text = "\n".join(lines)
    assert lines[0] == "TRADEHUB WATCH — FRESHNESS WINDOW RECOVERED"
    assert "Quarantined after remediation: unverified" in text
    assert "Quarantined after remediation: 0" not in text

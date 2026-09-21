"""Regression tests: rotation priority + the effective-budget diagnosis.

Two defects behind the 2026-09-21 "70 stale" degradation, both of which made the
system report a cause that was not the real one:

1. **Alphabetical rotation with a hard budget starves the tail.** The daily
   refresh built its candidate list with ``sorted(by_ticker)`` and stopped the
   moment the budget was spent, so the budget always went to whatever sorts
   first. Live: 144 candidates against a 74-request budget -- the run refreshed
   ``NPHC..SNROF`` and the 70 names *after* SNROF were not fetched. Those names
   are still stale (last bar 2026-09-09) after ten days of nightly refreshes,
   because every run restarted the walk at the head of the alphabet. The fix
   orders candidates by sessions-behind descending, so the budget is spent on
   the worst data and service rotates through the backlog instead of always
   beginning at the same place.

2. **The audit judged the budget against the floor constant.** ``audit_universe``
   defaulted ``budget`` to ``ROTATION_REQUESTS_PER_RUN`` (40) while the deployed
   refresh actually runs ``rotation_budget_for()`` (74 for the 443-name
   universe). ``40 < 74`` therefore labelled every backlogged name
   ``ROTATION_BUDGET_STARVED`` -- "a budget this deployment cannot deliver" --
   and told the operator "budget 40/day, needs 74/day" when the refresh was
   already doing 74/day. The real cause (candidates exceeding the budget) was
   hidden behind a false structural one.

Offline and deterministic: no network, no live database.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from tradehub_research.db import ResearchDB
from tradehub_research.ops import daily_refresh as dr
from tradehub_research.ops import data_freshness as df

AS_OF = date(2026, 9, 18)
#: ``count_sessions(as_of - 7d, as_of)`` -- the window in SESSIONS, not days.
WINDOW_SESSIONS = 6


# ---------------------------------------------------------------------------
# 1 -- rotation priority
# ---------------------------------------------------------------------------
class TestRotationPriority:
    def _wire(self, monkeypatch, bars: dict[str, str], known: set[str] | None = None):
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(
            dr, "symbol_has_evidence", lambda _db, ticker: known is None or ticker in known
        )

    def test_worst_data_wins_the_last_request(self, monkeypatch):
        """The 2026-09-21 shape: budget 1, and ZED is the one that needs it.

        MID is 7 sessions behind; ZED is 30. An alphabetical walk spends the
        request on MID and leaves ZED behind -- which is precisely how the tail
        starved while shorter-stale names kept getting served.
        """
        bars = {"S-MID": "2026-09-09", "S-ZED": "2026-08-01"}
        self._wire(monkeypatch, bars)
        ordered, skipped = dr.rotation_candidates(
            None,
            {"MID": "S-MID", "ZED": "S-ZED"},
            as_of=AS_OF,
            window_sessions=WINDOW_SESSIONS,
        )
        assert ordered == ["ZED", "MID"], "budget must be spent worst-first, not alphabetically"
        assert skipped == 0

    def test_names_inside_the_window_are_skipped_and_not_reordered(self, monkeypatch):
        """Fresh names cost no budget; the ordering only ranks real candidates."""
        bars = {"S-ACK": "2026-09-10", "S-MID": "2026-09-09", "S-ZED": "2026-08-01"}
        self._wire(monkeypatch, bars)
        ordered, skipped = dr.rotation_candidates(
            None,
            {"ACK": "S-ACK", "MID": "S-MID", "ZED": "S-ZED"},
            as_of=AS_OF,
            window_sessions=WINDOW_SESSIONS,
        )
        # 2026-09-10 is exactly 6 sessions behind -> inside the window.
        assert "ACK" not in ordered
        assert skipped == 1
        assert ordered == ["ZED", "MID"]

    def test_ticker_breaks_ties_so_the_walk_stays_deterministic(self, monkeypatch):
        bars = {"S-A": "2026-09-09", "S-B": "2026-09-09", "S-C": "2026-09-09"}
        self._wire(monkeypatch, bars)
        ordered, _skipped = dr.rotation_candidates(
            None,
            {"B": "S-B", "C": "S-C", "A": "S-A"},
            as_of=AS_OF,
            window_sessions=WINDOW_SESSIONS,
        )
        assert ordered == ["A", "B", "C"]

    def test_a_symbol_with_no_bars_is_served_before_merely_stale_ones(self, monkeypatch):
        """CHECKPOINT_LOST is the worst data state, not the least urgent.

        ``_sessions_behind`` reports "no bars" as the sentinel ``-1``. Ranking on
        that raw value would put the symbol with no data at all behind every
        symbol that merely holds an old bar -- and, whenever the positive-stale
        backlog is at least the budget, behind them forever.
        """
        bars = {"S-OLD": "2026-06-08"}  # S-NEW holds no bars at all
        self._wire(monkeypatch, bars)
        ordered, _skipped = dr.rotation_candidates(
            None,
            {"OLD": "S-OLD", "NEW": "S-NEW"},
            as_of=AS_OF,
            window_sessions=WINDOW_SESSIONS,
        )
        assert ordered == ["NEW", "OLD"]

    def test_retired_and_unresolvable_names_never_consume_a_request(self, monkeypatch):
        bars = {"S-DEAD": "2026-08-01", "S-UNKNOWN": "2026-08-01", "S-LIVE": "2026-08-01"}
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: ticker != "UNKNOWN")
        ordered, _skipped = dr.rotation_candidates(
            None,
            {"DEAD": "S-DEAD", "UNKNOWN": "S-UNKNOWN", "LIVE": "S-LIVE"},
            as_of=AS_OF,
            window_sessions=WINDOW_SESSIONS,
            retired={"DEAD"},
        )
        assert ordered == ["LIVE"]

    def test_a_short_budget_spends_itself_on_the_tail_end_to_end(self, tmp_path, monkeypatch):
        """The defect at the seam that produced it: ``run_daily_refresh`` itself.

        One request, three symbols, and the most-stale name sorts last. The
        request must go to ZED.
        """
        research_db = ResearchDB(tmp_path / "research.db", 5000)
        research_db.migrate()
        paths = SimpleNamespace(research_db=tmp_path / "research.db")
        settings = SimpleNamespace(
            busy_timeout_ms=5000,
            tiingo_token=None,
            tiingo_license_confirmed=True,
            adapter_cache_dir=tmp_path,
        )
        bars = {"S-ACK": "2026-09-10", "S-MID": "2026-09-09", "S-ZED": "2026-08-01"}
        monkeypatch.setattr(
            dr, "canonical_tickers_by_cik", lambda _db: {sid: sid[2:] for sid in bars}
        )
        monkeypatch.setattr(dr, "_last_bar_date", lambda _db, sid: bars.get(sid))
        monkeypatch.setattr(dr, "symbol_has_evidence", lambda _db, ticker: True)
        monkeypatch.setattr(dr, "_load_retired", set)
        monkeypatch.setattr(dr, "_active_securities", lambda _db, days=14: set())

        refreshed: list[str] = []
        monkeypatch.setattr(dr, "_refresh_one", lambda *a, **kw: refreshed.append(a[5]))

        class _Quota:
            def bootstrap_usage(self, _now, _limit):
                return {"used": 0, "symbols": []}

            def remaining(self):
                return {"hourly": 45}

        monkeypatch.setattr(dr, "TiingoEodAdapter", lambda **kw: SimpleNamespace(quota=_Quota()))

        summary = dr.run_daily_refresh(
            settings=settings,
            experiment_db=None,
            paths=paths,
            as_of=AS_OF,
            rotation_budget=1,
        )

        assert summary["rotation_refreshed"] == 1
        assert refreshed == ["ZED"], "the single request must land on the worst data"
        assert summary["rotation_candidates"] == 2  # MID + ZED


# ---------------------------------------------------------------------------
# 2 -- the effective budget in the diagnosis
# ---------------------------------------------------------------------------
def _wire_audit(monkeypatch, universe: int, bars: dict[str, str] | None = None):
    """Patch the audit's I/O seams so the budget arithmetic is what is tested."""
    import tradehub_research.backfill.tiingo_driver as driver
    import tradehub_research.db as dbmod

    monkeypatch.setattr(
        driver,
        "canonical_tickers_by_cik",
        lambda _db: {f"S{i:05d}": f"T{i:05d}" for i in range(universe)},
    )
    monkeypatch.setattr(driver, "symbol_has_evidence", lambda _db, _ticker: True)
    monkeypatch.setattr(dbmod, "ResearchDB", lambda *a, **k: object())
    monkeypatch.setattr(dr, "retired_tickers", set)
    monkeypatch.setattr(df, "_last_bar", lambda _db, sid: (bars or {}).get(sid, "2026-09-09"))
    monkeypatch.setattr(df, "_last_attempt", lambda _exp, _ticker: None)
    return SimpleNamespace(busy_timeout_ms=5000, tiingo_token=None, adapter_cache_dir="/tmp")


class TestEffectiveBudgetDiagnosis:
    def test_a_backlog_is_an_interrupted_batch_not_a_structural_shortfall(
        self, tmp_path, monkeypatch
    ):
        """443 names, 6-session window, refresh runs 74/day -- so 40 is not the budget.

        Every name is behind; none of them is behind because the deployment
        cannot deliver the contract. Classifying them ROTATION_BUDGET_STARVED
        told the operator the opposite of the truth.
        """
        settings = _wire_audit(monkeypatch, universe=443)
        audit = df.audit_universe(
            settings=settings,
            paths=SimpleNamespace(research_db=tmp_path / "research.db"),
            experiment_db=None,
            as_of=AS_OF,
        )
        assert audit.universe == 443
        disputed = {r.classification for r in audit.stale}
        assert df.ROTATION_STARVED not in disputed
        assert disputed == {df.INTERRUPTED_BATCH}

    def test_the_structural_shortfall_still_fires_when_it_is_real(self, tmp_path, monkeypatch):
        """Positive control: no universe size can hold a contract past the ceiling.

        5000 names inside a 6-session window needs 834/day; the rotation is
        capped at 200. That IS a budget the refresh cannot deliver, and it must
        still be reported as such.
        """
        from tradehub_research.ops.daily_refresh import ROTATION_REQUESTS_MAX

        settings = _wire_audit(monkeypatch, universe=5000)
        audit = df.audit_universe(
            settings=settings,
            paths=SimpleNamespace(research_db=tmp_path / "research.db"),
            experiment_db=None,
            as_of=AS_OF,
        )
        assert {r.classification for r in audit.stale} == {df.ROTATION_STARVED}
        notes = {r.notes for r in audit.stale}
        assert len(notes) == 1
        note = notes.pop() or ""
        assert f"{ROTATION_REQUESTS_MAX}/day" in note  # the real, effective budget
        assert "5000 symbols" in note

    def test_an_explicit_budget_override_still_wins(self, tmp_path, monkeypatch):
        """Callers that pin a budget keep judging against that budget."""
        settings = _wire_audit(monkeypatch, universe=443)
        audit = df.audit_universe(
            settings=settings,
            paths=SimpleNamespace(research_db=tmp_path / "research.db"),
            experiment_db=None,
            as_of=AS_OF,
            rotation_budget=10,
        )
        assert {r.classification for r in audit.stale} == {df.ROTATION_STARVED}
        assert "10/day" in (next(iter({r.notes for r in audit.stale})) or "")


@pytest.mark.parametrize("universe,expected", [(443, 74), (240, 40), (1200, 200), (5000, 200)])
def test_rotation_budget_matches_the_documented_clamp(universe, expected):
    """The number the diagnosis must use, straight from the refresh's own rule."""
    assert (
        dr.rotation_budget_for(universe, window_sessions=WINDOW_SESSIONS, as_of=AS_OF) == expected
    )

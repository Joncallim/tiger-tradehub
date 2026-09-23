"""Exact exit sessions, safe price conversion, and the two evaluation clocks.

Three gaps found in review of 5ffbdb1:

1. EXIT EXACTNESS. The live path requires a canonical bar for EXACTLY
   ``required_exit_session(entry, horizon)``; the frozen builder still used
   ``select_exit_bar``, which returns the N-th AVAILABLE bar. With a session
   missing, the two planes therefore selected different exit sessions.

2. PRICE SAFETY. ``outcome_prices.bar_open/bar_close`` used
   ``portfolio.prices._d`` (``Decimal(str(value))``) with no exception handling,
   so a null/absent/malformed price raised instead of falling back or reporting an
   honest pending state.

3. THE TWO CLOCKS. Live maturation used one date for two different concepts --
   "has the required US session elapsed" and "which publication timestamps are
   visible". ``market_calendar.expected_latest_session`` is exchange-local,
   holiday-aware and uses the 20:15 ET close+buffer boundary, which is exactly the
   REAL Tiingo PAT convention (``TiingoEodAdapter.parse``: 20:15 America/New_York
   -> UTC, so session 2026-09-30 publishes at 2026-10-01T00:15:00Z). An
   end-of-session-date UTC bound therefore excludes the genuine Sep-30 EOD bar.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths
from tradehub_research.ops.health import forward_health
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.horizons import entry_session_for, required_exit_session

SECURITY = "S1"
TICKER = "TST"
AS_OF = "2026-05-29"  # Friday
ENTRY = "2026-06-01"  # Monday
EXIT = required_exit_session(ENTRY, 21)  # 2026-07-01
FROZEN_BOUND = "9999-12-31T00:00:00Z"


def _tiingo_pat(session: str) -> str:
    """The REAL adapter convention: 20:15 America/New_York -> UTC."""
    local = datetime.combine(
        date.fromisoformat(session), time(20, 15), ZoneInfo("America/New_York")
    )
    return local.astimezone(timezone.utc).isoformat()


def _run_now(session: str) -> datetime:
    """The nightly run that can see ``session``'s EOD evidence.

    Scheduled runtime is Mon-Fri 23:45 (+08) = 15:45Z; the first run after a
    session's 20:15 ET publication is therefore 15:45Z on the FOLLOWING day.
    """
    return datetime.fromisoformat(f"{session}T15:45:00+00:00") + timedelta(days=1)


def _paths(tmp_path) -> ResearchPaths:
    return ResearchPaths(
        research_dir=tmp_path / "research",
        research_db=tmp_path / "research.db",
        experiment_db=tmp_path / "experiment.db",
        replay_db=tmp_path / "replay.db",
        snapshots_dir=tmp_path / "snapshots",
        artifacts_dir=tmp_path / "artifacts",
        raw_cache=tmp_path / "raw",
    )


def _settings(research_db: ResearchDB) -> ResearchSettings:
    return ResearchSettings(db_path=research_db.path, busy_timeout_ms=5000)


def _seed(tmp_path, *, with_snapshot: bool = False):
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    research_db = ResearchDB(tmp_path / "research.db")
    research_db.migrate()
    exp = ExperimentDB(tmp_path / "experiment.db", 5000)
    exp.migrate()
    with research_db.connect() as conn:
        conn.execute(
            "INSERT INTO evidence_source VALUES (?,?,?,?,?)",
            ("tiingo_eod", "market_data", 1, "test", "derived_from_index"),
        )
        conn.execute(
            "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
            (
                SECURITY,
                TICKER,
                "US",
                "Test Co",
                "Technology",
                "HW",
                "SUPPORTED",
                "2026-01-01T00:00:00Z",
                None,
            ),
        )
    if with_snapshot:
        with exp.connect() as conn:
            conn.execute(
                "INSERT INTO dataset_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "snap-1",
                    "abc",
                    11,
                    None,
                    "{}",
                    "h1",
                    "/tmp/x",
                    "h2",
                    "{}",
                    "READY",
                    "2025-01-01T00:00:00Z",
                ),
            )
            conn.commit()
    return _paths(tmp_path), research_db, exp


def _bar(
    research_db: ResearchDB,
    day: str,
    *,
    close: object = 100.0,
    open_price: object = 100.0,
    tag: str = "a",
    published: str | None = None,
    omit_open: bool = False,
    omit_close: bool = False,
) -> str:
    fields: dict = {
        "record_type": "price_bar",
        "provider_ticker": TICKER,
        "session_date": day,
        "high": 100.0,
        "low": 100.0,
        "volume": 1000,
    }
    if not omit_open:
        fields["open"] = open_price
    if not omit_close:
        fields["close"] = close
    return EvidenceStore(research_db).insert(
        security_id=SECURITY,
        source_id="tiingo_eod",
        structured_fields=fields,
        extraction_confidence=0.9,
        event_time=f"{day}T20:15:00Z",
        public_available_time=published or _tiingo_pat(day),
        # ingested when the evidence became available (must not precede its PAT)
        ingested_time=published or _tiingo_pat(day),
        pat_provenance="derived_from_index",
        source_record_id=f"{TICKER}:{day}:{tag}",
    )


def _prediction(exp: ExperimentDB, *, horizon: int = 21, as_of: str = AS_OF, pip: str = "P1"):
    from tradehub_research.validation.forward_collector import _outcome_due_date

    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_prediction(prediction_id, security_id, as_of, variant_name,"
            " score_value, state, screen_passed, sufficient_data, raw_features_hash, config_hash,"
            " evidence_ids_json, horizon_sessions, outcome_due_date, created_at, provenance)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pip,
                SECURITY,
                as_of,
                "production",
                0.5,
                "CA",
                1,
                1,
                "h",
                "c",
                "[]",
                horizon,
                _outcome_due_date(as_of, horizon),
                f"{as_of}T20:15:00Z",
                "production",
            ),
        )
        conn.commit()


def _run(exp, research_db, paths, now: datetime) -> dict:
    return mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=now,
    )


def _live_rows(exp: ExperimentDB) -> list[dict]:
    with exp.connect(read_only=True) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT prediction_id, outcome_status, raw_return, total_return,"
                " entry_session_date, exit_session_date FROM forward_outcome"
            ).fetchall()
        ]


def _frozen_label(research_db, exp, *, horizon: int = 21, observation: str = AS_OF) -> dict:
    from tradehub_research.validation.outcome_builder import build_outcome_label

    return build_outcome_label(
        research_db,
        exp,
        dataset_snapshot_id="snap-1",
        security_id=SECURITY,
        observation_date=observation,
        horizon_sessions=horizon,
    )


def _sessions_after(first: str, count: int) -> list[str]:
    out: list[str] = []
    day = date.fromisoformat(first)
    while len(out) < count:
        from tradehub_research.ops.market_calendar import is_session_day

        if is_session_day(day):
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


# ==================== 1. exact exit session in the frozen builder =================


def test_frozen_exit_uses_the_exact_required_session_despite_an_intermediate_gap(tmp_path):
    """An unrelated missing session must not move the frozen exit."""
    paths, rdb, exp = _seed(tmp_path, with_snapshot=True)
    _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="entry")
    _bar(rdb, EXIT, close=121.0, open_price=121.0, tag="exit")
    _prediction(exp)

    label = _frozen_label(rdb, exp)
    assert label["outcome_status"] == "OBSERVED", label
    assert label["exit_session_date"] == EXIT, label


def test_frozen_exit_does_not_slide_to_the_21st_available_bar(tmp_path):
    """20 sessions + a missing intermediate + later bars: the old N-th-available
    rule would move the exit PAST the required session."""
    paths, rdb, exp = _seed(tmp_path, with_snapshot=True)
    sessions = _sessions_after(ENTRY, 22)  # entry + 21
    _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="entry")
    for offset, day in enumerate(sessions[1:], start=1):
        if day == sessions[10]:  # one intermediate session has no bar
            continue
        _bar(rdb, day, close=100.0 + offset, open_price=100.0 + offset, tag=f"s{offset}")
    for offset, day in enumerate(_sessions_after(EXIT, 4)[1:], start=1):
        _bar(rdb, day, close=300.0 + offset, open_price=300.0 + offset, tag=f"post{offset}")
    _prediction(exp)

    label = _frozen_label(rdb, exp)
    assert label["outcome_status"] == "OBSERVED", label
    assert label["exit_session_date"] == EXIT, "the exit must be the required session"


def test_frozen_exit_is_never_shifted_when_the_required_session_bar_is_missing(tmp_path):
    """Required exit bar absent, later bars present: frozen must not shift later."""
    paths, rdb, exp = _seed(tmp_path, with_snapshot=True)
    _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="entry")
    # bars AFTER the required exit session exist, but not the required one
    for offset, day in enumerate(_sessions_after(EXIT, 6)[1:], start=1):
        _bar(rdb, day, close=200.0 + offset, open_price=200.0 + offset, tag=f"late{offset}")
    _prediction(exp)

    label = _frozen_label(rdb, exp)
    assert label["outcome_status"] == "CENSORED_INSUFFICIENT_HORIZON", label
    assert label["exit_session_date"] is None, "the frozen exit must never shift later"


def test_live_equivalent_of_a_missing_exact_exit_stays_pending(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="entry")
    for offset, day in enumerate(_sessions_after(EXIT, 6)[1:], start=1):
        _bar(rdb, day, close=200.0 + offset, open_price=200.0 + offset, tag=f"late{offset}")
    _prediction(exp)

    summary = _run(exp, rdb, paths, _run_now(EXIT))
    assert _live_rows(exp) == [], summary
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1, summary


def test_both_planes_emit_the_same_exit_when_both_observe(tmp_path):
    paths, rdb, exp = _seed(tmp_path, with_snapshot=True)
    for day, close in ((ENTRY, 100.0), (EXIT, 121.0)):
        _bar(rdb, day, close=close, open_price=close, tag=f"bar-{day}")
    _prediction(exp)

    label = _frozen_label(rdb, exp)
    _run(exp, rdb, paths, _run_now(EXIT))
    rows = _live_rows(exp)
    assert label["outcome_status"] == "OBSERVED"
    assert rows and rows[0]["outcome_status"] == "OBSERVED"
    assert label["exit_session_date"] == rows[0]["exit_session_date"] == EXIT
    assert label["entry_session_date"] == rows[0]["entry_session_date"] == ENTRY
    assert float(label["raw_return"]) == pytest.approx(rows[0]["raw_return"], rel=1e-9)
    assert float(label["total_return"]) == pytest.approx(rows[0]["total_return"], rel=1e-9)


# ======================== 2. null / malformed price safety ========================


def test_absent_open_falls_back_to_the_close(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, omit_open=True, tag="entry-no-open")
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, _run_now(EXIT))
    rows = _live_rows(exp)
    assert rows and rows[0]["outcome_status"] == "OBSERVED", rows
    assert rows[0]["raw_return"] == pytest.approx(121.0 / 100.0 - 1.0)


def test_null_open_falls_back_to_the_close(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, open_price=None, tag="entry-null-open")
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, _run_now(EXIT))
    rows = _live_rows(exp)
    assert rows and rows[0]["outcome_status"] == "OBSERVED", rows
    assert rows[0]["raw_return"] == pytest.approx(121.0 / 100.0 - 1.0)


def test_malformed_open_falls_back_to_the_close(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, open_price="n/a", tag="entry-bad-open")
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, _run_now(EXIT))
    rows = _live_rows(exp)
    assert rows and rows[0]["outcome_status"] == "OBSERVED", rows
    assert rows[0]["raw_return"] == pytest.approx(121.0 / 100.0 - 1.0)


def test_absent_or_malformed_close_without_a_valid_open_is_an_entry_gap(tmp_path):
    """Live: AWAITING_ENTRY_BAR. Frozen: ENTRY_UNAVAILABLE. Neither crashes."""
    for kw in ({"omit_close": True}, {"close": None}, {"close": "n/a"}):
        sub = tmp_path / f"case-{list(kw)[0]}-{str(list(kw.values())[0])}"
        paths, rdb, exp = _seed(sub, with_snapshot=True)
        _bar(rdb, ENTRY, open_price=0.0, tag="entry-unusable", **kw)
        _bar(rdb, EXIT, close=121.0, tag="exit")
        _prediction(exp)

        summary = _run(exp, rdb, paths, _run_now(EXIT))
        assert _live_rows(exp) == [], (kw, summary)
        assert summary["awaiting"]["AWAITING_ENTRY_BAR"] == 1, (kw, summary)
        assert _frozen_label(rdb, exp)["outcome_status"] == "ENTRY_UNAVAILABLE", kw


def test_malformed_exit_close_is_an_exit_gap_and_never_observed(tmp_path):
    for kw in ({"omit_close": True}, {"close": None}, {"close": "oops"}):
        sub = tmp_path / f"exit-{list(kw)[0]}-{str(list(kw.values())[0])}"
        paths, rdb, exp = _seed(sub, with_snapshot=True)
        _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="entry")
        _bar(rdb, EXIT, tag="exit-unusable", **kw)
        _prediction(exp)

        summary = _run(exp, rdb, paths, _run_now(EXIT))
        assert _live_rows(exp) == [], (kw, summary)
        assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1, (kw, summary)
        assert _frozen_label(rdb, exp)["outcome_status"] != "OBSERVED", kw


# ========================= 3. market-session vs visibility clock ==================


def test_the_real_tiingo_pat_convention_is_the_20_15_et_boundary():
    """Pin the adapter's convention we must respect (not change)."""
    assert _tiingo_pat("2026-09-30") == "2026-10-01T00:15:00+00:00"
    assert _tiingo_pat("2026-11-25") == "2026-11-26T01:15:00+00:00"  # EST: +5


def test_sep30_session_is_not_mature_at_sep30_end_of_day(tmp_path):
    """Before the Sep-30 close+buffer the Sep-30 horizon has NOT elapsed."""
    paths, rdb, exp = _seed(tmp_path)
    cohort = "2026-08-28"
    entry = entry_session_for(cohort)
    exit_session = required_exit_session(entry, 21)
    assert (entry, exit_session) == ("2026-08-31", "2026-09-30")
    _bar(rdb, entry, close=100.0, open_price=100.0, tag="entry")
    _bar(rdb, exit_session, close=110.0, open_price=110.0, tag="exit")
    _prediction(exp, as_of=cohort)

    summary = _run(exp, rdb, paths, datetime.fromisoformat("2026-09-30T15:45:00+00:00"))
    assert _live_rows(exp) == [], summary
    assert summary["awaiting"]["AWAITING_HORIZON"] == 1, summary
    assert summary["session_cutoff"] == "2026-09-29", summary


def test_sep30_end_of_day_utc_is_still_not_visible(tmp_path):
    """23:59:59Z on the session date is BEFORE the real PAT (00:15Z next day)."""
    paths, rdb, exp = _seed(tmp_path)
    cohort = "2026-08-28"
    entry = entry_session_for(cohort)
    exit_session = required_exit_session(entry, 21)
    _bar(rdb, entry, close=100.0, open_price=100.0, tag="entry")
    _bar(rdb, exit_session, close=110.0, open_price=110.0, tag="exit")
    _prediction(exp, as_of=cohort)

    summary = _run(exp, rdb, paths, datetime.fromisoformat("2026-09-30T23:59:59+00:00"))
    assert _live_rows(exp) == [], summary
    assert summary["awaiting"]["AWAITING_HORIZON"] == 1, summary
    assert summary["session_cutoff"] == "2026-09-29", summary


def test_the_oct1_singapore_night_run_matures_the_sep30_session(tmp_path):
    """15:45Z on Oct-1: Sep-30 is the completed session AND its PAT is visible."""
    paths, rdb, exp = _seed(tmp_path)
    cohort = "2026-08-28"
    entry = entry_session_for(cohort)
    exit_session = required_exit_session(entry, 21)
    _bar(rdb, entry, close=100.0, open_price=100.0, tag="entry")
    _bar(rdb, exit_session, close=110.0, open_price=110.0, tag="exit")  # PAT Oct-1 00:15Z
    _prediction(exp, as_of=cohort)

    summary = _run(exp, rdb, paths, datetime.fromisoformat("2026-10-01T15:45:00+00:00"))
    rows = _live_rows(exp)
    assert summary["session_cutoff"] == "2026-09-30", summary
    assert rows and rows[0]["outcome_status"] == "OBSERVED", (summary, rows)
    assert rows[0]["entry_session_date"] == "2026-08-31"
    assert rows[0]["exit_session_date"] == "2026-09-30"
    assert rows[0]["raw_return"] == pytest.approx(110.0 / 100.0 - 1.0)


def test_maturation_and_health_agree_on_the_same_injected_now(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    cohort = "2026-08-28"
    entry = entry_session_for(cohort)
    exit_session = required_exit_session(entry, 21)
    _bar(rdb, entry, close=100.0, open_price=100.0, tag="entry")
    _bar(rdb, exit_session, close=110.0, open_price=110.0, tag="exit")
    _prediction(exp, as_of=cohort)

    for moment, expected_reason in (
        ("2026-09-30T15:45:00+00:00", "AWAITING_HORIZON"),
        ("2026-09-30T23:59:59+00:00", "AWAITING_HORIZON"),
        ("2026-10-01T15:45:00+00:00", None),
    ):
        now = datetime.fromisoformat(moment)
        h = forward_health(experiment_db=exp, paths=paths, now=now)
        if expected_reason is None:
            assert h["session_cutoff"] == "2026-09-30", (moment, h)
        if expected_reason is None:
            assert h["predictions_due"] == 1, (moment, h)
        else:
            assert h["predictions_due"] == 0, (moment, h)
            assert h["awaiting_horizon"]["total"] == 1, (moment, h)

    # and the maturation, replayed at the same moments, classifies identically
    summary = _run(exp, rdb, paths, datetime.fromisoformat("2026-09-30T23:59:59+00:00"))
    assert summary["awaiting"]["AWAITING_HORIZON"] == 1
    assert _live_rows(exp) == []
    _run(exp, rdb, paths, datetime.fromisoformat("2026-10-01T15:45:00+00:00"))
    assert _live_rows(exp)[0]["outcome_status"] == "OBSERVED"


def test_the_holiday_boundary_never_yields_a_non_session(tmp_path):
    """Thanksgiving 2026-11-26: the completed session is Wednesday 11-25."""
    paths, rdb, exp = _seed(tmp_path)
    now = datetime.fromisoformat("2026-11-26T15:45:00+00:00")  # 10:45 ET on the holiday
    h = forward_health(experiment_db=exp, paths=paths, now=now)
    assert h["session_cutoff"] == "2026-11-25", h

    weekend = forward_health(
        experiment_db=exp, paths=paths, now=datetime.fromisoformat("2026-11-29T15:45:00+00:00")
    )
    assert weekend["session_cutoff"] == "2026-11-27", weekend  # Friday, not the weekend


def test_a_later_published_correction_stays_invisible_until_its_pat(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    cohort = "2026-08-28"
    entry = entry_session_for(cohort)
    exit_session = required_exit_session(entry, 21)
    _bar(rdb, entry, close=100.0, open_price=100.0, tag="entry")
    original = _bar(rdb, exit_session, close=110.0, open_price=110.0, tag="exit")
    _prediction(exp, as_of=cohort)

    # a correction published on Oct-2 (its own PAT), superseding the original
    EvidenceStore(rdb).insert(
        security_id=SECURITY,
        source_id="tiingo_eod",
        structured_fields={
            "record_type": "price_bar",
            "provider_ticker": TICKER,
            "session_date": exit_session,
            "open": 120.0,
            "high": 120.0,
            "low": 120.0,
            "close": 120.0,
            "volume": 1000,
        },
        extraction_confidence=0.9,
        event_time=f"{exit_session}T20:15:00Z",
        public_available_time="2026-10-02T00:15:00+00:00",
        ingested_time="2026-10-02T00:20:00+00:00",
        pat_provenance="derived_from_index",
        source_record_id=f"{TICKER}:{exit_session}:correction",
        supersedes_evidence_id=original,
    )

    # the Oct-1 run cannot see it yet -> uses the original 110
    _run(exp, rdb, paths, datetime.fromisoformat("2026-10-01T15:45:00+00:00"))
    rows = _live_rows(exp)
    assert rows[0]["raw_return"] == pytest.approx(110.0 / 100.0 - 1.0), rows

    # and a later run sees exactly the same thing (one immutable outcome)
    assert len(rows) == 1

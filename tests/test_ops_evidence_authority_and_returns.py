"""Evidence-chain authority, canonical returns, and "due" semantics.

Three correctness properties of the LIVE forward ledger, each of which the
pre-refactor code violated:

1. EVIDENCE AUTHORITY. The live maturation used to read ``evidence_event`` rows
   directly and hand them to ``prices._bar_records`` -- bypassing
   ``prices._visible_records``, which owns supersession resolution, withdrawn
   terminals, publication visibility and approved PAT provenance. A superseding
   CORRECTION was therefore seen as a conflicting same-session duplicate (session
   dropped), and a WITHDRAWN successor simply vanished (excluded by the
   ``record_type='price_bar'`` predicate), resurrecting the superseded original.

2. CANONICAL RETURNS. The live evaluator computed ``exit_close / entry_close - 1``
   on the entry-session CLOSE. The canonical Phase-5 contract is: entry = next
   eligible session with the OPEN preferred (explicit CLOSE fallback), exit = the
   exact horizon session's CLOSE, ``raw_return`` unadjusted, ``total_return``
   corporate-action aware.

3. DUE SEMANTICS. Legacy immutable rows carry an advisory ``outcome_due_date``
   (2026-09-27 for the first cohort) that precedes the genuine 21-session maturity
   (2026-09-30). The advisory gate must never be reported as outcomes that are
   actually due.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tradehub_research.config import ResearchSettings
from tradehub_research.db import ResearchDB
from tradehub_research.evidence import EvidenceStore
from tradehub_research.ops.common import ResearchPaths
from tradehub_research.ops.health import forward_health
from tradehub_research.ops.outcome_maturation import mature_due_outcomes
from tradehub_research.validation.experiment_db import ExperimentDB
from tradehub_research.validation.horizons import entry_session_for, required_exit_session


def _night_after(session_date: str) -> datetime:
    """The scheduled 23:45 (+08) = 15:45Z run that can see `session_date`'s EOD.

    The real Tiingo PAT for a US session is 20:15 ET -> UTC (the next UTC day), so
    the first run able to use that session is the following night's.
    """
    return datetime.fromisoformat(f"{session_date}T15:45:00+00:00") + timedelta(days=1)


SECURITY = "S1"
TICKER = "TST"
AS_OF = "2026-05-29"  # Friday
ENTRY = "2026-06-01"  # the Monday the calendar expects
EXIT = required_exit_session(ENTRY, 21)  # 2026-07-01


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


def _seed(tmp_path):
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
    return _paths(tmp_path), research_db, exp


def _bar(
    research_db: ResearchDB,
    day: str,
    *,
    close: float,
    open_price: float | None = None,
    tag: str = "a",
    published: str | None = None,
    pat: str = "source_reported",
    supersedes: str | None = None,
    withdrawn: bool = False,
    fields: dict | None = None,
) -> str:
    structured = (
        fields
        if fields is not None
        else {
            "record_type": "price_bar",
            "provider_ticker": TICKER,
            "session_date": day,
            "open": open_price if open_price is not None else close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000,
        }
    )
    return EvidenceStore(research_db).insert(
        security_id=SECURITY,
        source_id="tiingo_eod",
        structured_fields=structured,
        extraction_confidence=0.9,
        event_time=f"{day}T20:15:00Z",
        public_available_time=published or f"{day}T20:15:00Z",
        pat_provenance=pat,
        source_record_id=f"{TICKER}:{day}:{tag}",
        supersedes_evidence_id=supersedes,
        withdrawn=withdrawn,
    )


def _action(research_db: ResearchDB, *, kind: str, day: str, value: float, tag: str) -> str:
    field = "factor" if kind == "split" else "cash"
    return EvidenceStore(research_db).insert(
        security_id=SECURITY,
        source_id="tiingo_eod",
        structured_fields={
            "record_type": kind,
            "provider_ticker": TICKER,
            "effective_date": day,
            field: value,
        },
        extraction_confidence=0.9,
        event_time=f"{day}T20:15:00Z",
        public_available_time=f"{day}T20:15:00Z",
        pat_provenance="source_reported",
        source_record_id=f"{TICKER}:{kind}:{day}:{tag}",
    )


def _prediction(exp: ExperimentDB, *, horizon: int = 21, pip: str = "P1", due: str | None = None):
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
                AS_OF,
                "production",
                0.5,
                "CA",
                1,
                1,
                "h",
                "c",
                "[]",
                horizon,
                due or _outcome_due_date(AS_OF, horizon),
                f"{AS_OF}T20:15:00Z",
                "production",
            ),
        )
        conn.commit()


def _run(exp, research_db, paths, collection: str) -> dict:
    return mature_due_outcomes(
        settings=_settings(research_db),
        experiment_db=exp,
        paths=paths,
        now=_night_after(collection),
    )


def _rows(exp: ExperimentDB) -> list[dict]:
    with exp.connect(read_only=True) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT prediction_id, outcome_status, raw_return, total_return,"
                " entry_session_date, exit_session_date FROM forward_outcome"
            ).fetchall()
        ]


# =============================== 1. evidence authority =========================


def test_a_superseding_correction_wins_over_the_original(tmp_path):
    """Visible corrected successor must be selected, not treated as a conflict."""
    paths, rdb, exp = _seed(tmp_path)
    original = _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="orig")
    _bar(rdb, ENTRY, close=150.0, open_price=150.0, tag="correction", supersedes=original)
    _bar(rdb, EXIT, close=150.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    rows = _rows(exp)
    assert len(rows) == 1, rows
    assert rows[0]["outcome_status"] == "OBSERVED"
    # entry price is the CORRECTED open (150), not the original (100)
    assert rows[0]["raw_return"] == pytest.approx(150.0 / 150.0 - 1.0)


def test_a_withdrawn_successor_never_resurrects_the_predecessor(tmp_path):
    """Withdrawal resolves the chain to NO record -- the original must not return."""
    paths, rdb, exp = _seed(tmp_path)
    original = _bar(rdb, ENTRY, close=100.0, open_price=100.0, tag="orig")
    # a withdrawal record (empty structured fields) supersedes the original
    _bar(rdb, ENTRY, close=0.0, tag="withdrawal", supersedes=original, withdrawn=True, fields={})
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    summary = _run(exp, rdb, paths, EXIT)
    assert _rows(exp) == [], f"a withdrawn chain must not produce an outcome: {summary}"
    assert summary["awaiting"]["AWAITING_ENTRY_BAR"] == 1


def test_a_correction_published_later_is_not_consumed_early(tmp_path):
    """Publication visibility: evidence published after the collection day waits."""
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, tag="entry")
    _bar(rdb, EXIT, close=121.0, tag="exit", published=f"{EXIT}T20:15:00Z")
    _prediction(exp)

    # 1) the exit bar published on the collection day IS visible (nightly run)
    _run(exp, rdb, paths, EXIT)
    assert len(_rows(exp)) == 1
    assert _rows(exp)[0]["outcome_status"] == "OBSERVED"

    # 2) a NOT-YET-PUBLISHED exit bar stays pending, never consumed early
    paths2, rdb2, exp2 = _seed(tmp_path / "late")
    _bar(rdb2, ENTRY, close=100.0, tag="entry")
    late = (date.fromisoformat(EXIT) + timedelta(days=2)).isoformat()
    _bar(rdb2, EXIT, close=121.0, tag="exit", published=f"{late}T20:15:00Z")
    _prediction(exp2)
    early = EXIT
    summary = _run(exp2, rdb2, paths2, early)
    assert _rows(exp2) == [], f"not-yet-published evidence must not be consumed: {summary}"
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1
    # ...and the same prediction matures once the correction is visible
    _run(exp2, rdb2, paths2, late)
    assert len(_rows(exp2)) == 1
    assert _rows(exp2)[0]["outcome_status"] == "OBSERVED"


def test_unapproved_pat_provenance_is_never_consumed(tmp_path):
    """'unknown' is storable but NOT an approved provenance for outcomes."""
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, tag="entry")
    _bar(rdb, EXIT, close=121.0, tag="exit", pat="unknown")
    _prediction(exp)

    summary = _run(exp, rdb, paths, EXIT)
    assert _rows(exp) == [], f"unapproved provenance must not be consumed: {summary}"
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1


def test_identical_duplicate_terminal_evidence_still_collapses(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, tag="entry-1")
    _bar(rdb, ENTRY, close=100.0, tag="entry-2")  # byte-identical duplicate session
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    rows = _rows(exp)
    assert len(rows) == 1
    assert rows[0]["outcome_status"] == "OBSERVED"


# ============================ 2. canonical returns ============================


def test_positive_next_session_open_is_the_entry_price(tmp_path):
    """open (110) differs from close (100): the canonical entry is the OPEN."""
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, open_price=110.0, tag="entry")
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    rows = _rows(exp)
    assert rows[0]["outcome_status"] == "OBSERVED"
    assert rows[0]["raw_return"] == pytest.approx(121.0 / 110.0 - 1.0)
    assert rows[0]["total_return"] == pytest.approx(121.0 / 110.0 - 1.0)


def test_missing_or_invalid_open_falls_back_to_the_close(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, open_price=0.0, tag="entry-no-open")
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    rows = _rows(exp)
    assert rows[0]["outcome_status"] == "OBSERVED"
    assert rows[0]["raw_return"] == pytest.approx(121.0 / 100.0 - 1.0)


def test_a_two_for_one_split_inside_the_horizon_is_not_minus_fifty_percent(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=200.0, open_price=200.0, tag="entry")
    _bar(rdb, EXIT, close=100.5, tag="exit")
    _action(rdb, kind="split", day="2026-06-15", value=2.0, tag="split")
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    row = _rows(exp)[0]
    assert row["outcome_status"] == "OBSERVED"
    assert row["raw_return"] == pytest.approx(100.5 / 200.0 - 1.0)  # ~ -49.75% raw
    assert row["total_return"] == pytest.approx((100.5 * 2.0) / 200.0 - 1.0)  # ~ +0.5%
    assert row["total_return"] > 0 > row["raw_return"]


def test_a_cash_dividend_inside_the_horizon_is_in_the_total_return(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, tag="entry")
    _bar(rdb, EXIT, close=100.0, tag="exit")
    _action(rdb, kind="dividend", day="2026-06-20", value=2.0, tag="div")
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    row = _rows(exp)[0]
    assert row["raw_return"] == pytest.approx(0.0)
    assert row["total_return"] == pytest.approx(0.02)


def test_a_withdrawn_corporate_action_is_not_applied(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=200.0, open_price=200.0, tag="entry")
    _bar(rdb, EXIT, close=100.5, tag="exit")
    split = _action(rdb, kind="split", day="2026-06-15", value=2.0, tag="split")
    EvidenceStore(rdb).insert(
        security_id=SECURITY,
        source_id="tiingo_eod",
        structured_fields={},
        extraction_confidence=0.9,
        event_time="2026-06-16T20:15:00Z",
        public_available_time="2026-06-16T20:15:00Z",
        pat_provenance="source_reported",
        source_record_id="withdraw-split",
        supersedes_evidence_id=split,
        withdrawn=True,
    )
    _prediction(exp)

    _run(exp, rdb, paths, EXIT)
    row = _rows(exp)[0]
    assert row["outcome_status"] == "OBSERVED"
    # the withdrawn split must NOT be applied: total == raw
    assert row["total_return"] == pytest.approx(row["raw_return"])


# =========================== invalid price handling ==========================


def test_unusable_entry_price_is_an_entry_gap_not_an_exit_gap(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=0.0, open_price=0.0, tag="entry-bad")
    _bar(rdb, EXIT, close=121.0, tag="exit")
    _prediction(exp)

    summary = _run(exp, rdb, paths, EXIT)
    assert _rows(exp) == []
    assert summary["awaiting"]["AWAITING_ENTRY_BAR"] == 1
    assert "AWAITING_EXIT_BAR" not in summary["awaiting"]


def test_unusable_exit_price_is_an_exit_gap_and_never_observed(tmp_path):
    paths, rdb, exp = _seed(tmp_path)
    _bar(rdb, ENTRY, close=100.0, tag="entry")
    _bar(rdb, EXIT, close=-5.0, tag="exit-bad")
    _prediction(exp)

    summary = _run(exp, rdb, paths, EXIT)
    assert _rows(exp) == [], "an outcome must never be derived from a non-positive price"
    assert summary["awaiting"]["AWAITING_EXIT_BAR"] == 1


# ============================= 3. "due" semantics ============================


def test_advisory_gate_is_not_reported_as_genuinely_due(tmp_path):
    """Legacy shape: advisory due 2026-09-27, genuine 21-session maturity 09-30."""
    paths, rdb, exp = _seed(tmp_path)
    as_of = "2026-08-28"  # Friday
    entry = entry_session_for(as_of)  # Monday 2026-08-31
    exit_session = required_exit_session(entry, 21)  # Wednesday 2026-09-30
    advisory = (date.fromisoformat(as_of) + timedelta(days=30)).isoformat()  # 2026-09-27
    assert advisory < exit_session

    with exp.connect() as conn:
        conn.execute(
            "INSERT INTO forward_prediction(prediction_id, security_id, as_of, variant_name,"
            " score_value, state, screen_passed, sufficient_data, raw_features_hash, config_hash,"
            " evidence_ids_json, horizon_sessions, outcome_due_date, created_at, provenance)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "LEGACY1",
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
                21,
                advisory,
                f"{as_of}T20:15:00Z",
                "production",
            ),
        )
        conn.commit()
    _bar(rdb, entry, close=100.0, tag="entry")

    def health(day: str) -> dict:
        return forward_health(experiment_db=exp, paths=paths, now=_night_after(day))

    for day in ("2026-09-27", "2026-09-28", "2026-09-29"):
        h = health(day)
        assert h["predictions_due_check"] == 1, day  # advisory gate is open
        assert h["predictions_due"] == 0, f"{day}: an immature horizon is not due"
        assert h["awaiting_horizon"]["total"] == 1 or h["awaiting_exit"]["total"] == 1

    h = health("2026-09-30")
    assert h["predictions_due_check"] == 1
    assert h["predictions_due"] == 1, "the 21-session horizon genuinely matures on 2026-09-30"


def test_report_never_describes_an_immature_horizon_as_due(tmp_path):
    from tradehub_research.ops.report_cli import _system_health

    healthy_refresh = {"stale_count": 0}
    immature = {
        "production_predictions": 2658,
        "predictions_due": 0,
        "predictions_due_check": 2658,
    }
    text = _system_health(immature, healthy_refresh, 1)
    assert "2658 scheduled for maturity check (horizon not elapsed)" in text
    assert "due" not in text, text
    assert "outcomes mature" not in text, text

    mature = {
        "production_predictions": 2658,
        "predictions_due": 2658,
        "predictions_due_check": 2658,
    }
    assert "2658 outcomes mature" in _system_health(mature, healthy_refresh, 1)


def test_due_counts_are_predictions_not_grouped_rows(tmp_path):
    """Variants of one prediction share a classification; the COUNTS must still
    report predictions (the health pass evaluates each unit once)."""
    paths, rdb, exp = _seed(tmp_path)
    as_of = "2026-08-28"
    entry = entry_session_for(as_of)
    exit_session = required_exit_session(entry, 21)
    advisory = (date.fromisoformat(as_of) + timedelta(days=30)).isoformat()

    with exp.connect() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO forward_prediction(prediction_id, security_id, as_of, variant_name,"
                " score_value, state, screen_passed, sufficient_data, raw_features_hash,"
                " config_hash, evidence_ids_json, horizon_sessions, outcome_due_date, created_at,"
                " provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"V{i}",
                    SECURITY,
                    as_of,
                    f"production/{i}",
                    0.5,
                    "CA",
                    1,
                    1,
                    "h",
                    "c",
                    "[]",
                    21,
                    advisory,
                    f"{as_of}T20:15:00Z",
                    "production",
                ),
            )
        conn.commit()
    _bar(rdb, entry, close=100.0, tag="entry")

    before = forward_health(experiment_db=exp, paths=paths, now=_night_after("2026-09-27"))
    assert before["predictions_due_check"] == 3, before
    assert before["predictions_due"] == 0, before

    at_gate = forward_health(experiment_db=exp, paths=paths, now=_night_after(exit_session))
    assert at_gate["predictions_due"] == 3, at_gate

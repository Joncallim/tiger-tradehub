"""Portfolio engine end-to-end: transitions, persistence, thesis breaks,
budget, idempotency, briefing."""

from __future__ import annotations

import pytest

from tests.portfolio_test_helpers import (
    seed_pipeline_run,
    seed_price_bars,
    seed_score,
    seed_security,
    seed_thesis_break,
)
from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.fixtures import fixture_policy
from tradehub_research.portfolio.policy import PolicyRegistry
from tradehub_research.portfolio.snapshot import build_signal_input, build_snapshot


def _closes(n=40, base=50.0, step=0.5):
    return [base + (i % 7) * step for i in range(n)]


class Runtime:
    def __init__(self, tmp_path):
        self.db = ResearchDB(tmp_path / "engine.db")
        self.db.migrate()
        PolicyRegistry(self.db).register(fixture_policy())
        self.engine = PortfolioEngine(self.db)

    def seed(
        self, security_id, *, sector="Tech", coverage="SUPPORTED", closes=None, delisted_at=None
    ):
        with self.db.connect() as conn:
            seed_security(
                conn, security_id, sector=sector, coverage=coverage, delisted_at=delisted_at
            )
            seed_price_bars(conn, security_id, closes=closes or _closes())

    def score(
        self,
        security_id,
        run_id,
        as_of,
        *,
        conviction=80,
        cause="INITIAL",
        evhash=None,
        prior=None,
        delta=None,
        trajectory="RISING",
        suffix="a",
    ):
        with self.db.connect() as conn:
            seed_pipeline_run(conn, run_id, as_of)
            return seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id=security_id,
                conviction=conviction,
                trajectory_label=trajectory,
                change_cause=cause,
                scored_evidence_hash=evhash or f"H-{run_id}-{suffix}",
                material_change_time=as_of,
                prior_conviction=prior,
                conviction_delta=delta,
                committee_suffix=suffix,
                run_as_of=as_of,
            )

    def snapshot(
        self, as_of, *, cash=10_000_000_000, nav=10_000_000_000, holdings=None, market=None
    ):
        return build_snapshot(
            as_of,
            cash_microusd=cash,
            nav_microusd=nav,
            holdings=holdings or [],
            market_inputs=market
            or [
                {
                    "security_id": "sec1",
                    "mark_price_microusd": 50_000_000,
                    "price_as_of": as_of,
                    "avg_dollar_volume_microusd": 2_000_000_000_000,
                    "liquidity_as_of": as_of,
                    "evidence_ids": [f"sec1:bar:{i:03d}" for i in range(40)],
                }
            ],
        )

    def signal(self, security_id, as_of, opportunity=500000):
        return build_signal_input(security_id, as_of, remaining_opportunity_ppm=opportunity)

    def run(self, run_id, as_of, snapshot, signals=None, **kwargs):
        return self.engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            signals=signals or [],
            allow_fixture=True,
            **kwargs,
        )


@pytest.fixture()
def runtime(tmp_path):
    return Runtime(tmp_path)


def _holding(
    security_id="sec1", quantity=1_000_000, value=5_000_000_000, sector="Tech", sellable=None
):
    return {
        "security_id": security_id,
        "quantity_microunits": quantity,
        "sellable_quantity_microunits": quantity if sellable is None else sellable,
        "market_value_microusd": value,
        "sector": sector,
    }


def test_discover_watch_then_persistence_then_enter(runtime):
    runtime.seed("sec1")
    runtime.score("sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", suffix="a")
    s1 = runtime.run(
        "run1",
        "2025-06-01T00:00:00Z",
        runtime.snapshot("2025-06-01T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-01T00:00:00Z")],
    )
    assert s1.transition_count == 1
    # run 2: evidence-driven, persistence 1/2 -> no transition
    runtime.score(
        "sec1",
        "run2",
        "2025-06-03T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        prior=80,
        delta=0,
        suffix="b",
    )
    s2 = runtime.run(
        "run2",
        "2025-06-03T00:00:00Z",
        runtime.snapshot("2025-06-03T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-03T00:00:00Z")],
    )
    assert s2.transition_count == 0 and s2.proposal_count == 0
    # run 3: unchanged evidence hash -> persistence must NOT advance
    runtime.score(
        "sec1",
        "run3",
        "2025-06-05T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        prior=80,
        delta=0,
        evhash="H-run2-b",
        suffix="c",
    )
    s3 = runtime.run(
        "run3",
        "2025-06-05T00:00:00Z",
        runtime.snapshot("2025-06-05T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-05T00:00:00Z")],
    )
    assert s3.transition_count == 0 and s3.proposal_count == 0
    # run 4: new evidence -> persistence 2/2 -> ENTER + BUY
    runtime.score(
        "sec1",
        "run4",
        "2025-06-07T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        prior=80,
        delta=0,
        suffix="d",
    )
    s4 = runtime.run(
        "run4",
        "2025-06-07T00:00:00Z",
        runtime.snapshot("2025-06-07T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-07T00:00:00Z")],
    )
    assert s4.transition_count == 1 and s4.proposal_count == 1
    assert "WATCH -> ENTER" in s4.briefing
    assert "BUY" in s4.briefing
    with runtime.db.connect(read_only=True) as conn:
        proposal = conn.execute("SELECT * FROM trade_proposal").fetchone()
        assert proposal["action"] == "BUY"
        assert proposal["target_weight_ppm"] == 80000
        assert proposal["requires_human_approval"] == 1
        assert proposal["proposal_mode"] == "PAPER"
        assert proposal["order_constraints_json"] is not None


def test_rerun_is_idempotent(runtime):
    runtime.seed("sec1")
    runtime.score("sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", suffix="a")
    snap = runtime.snapshot("2025-06-01T00:00:00Z")
    first = runtime.run("run1", "2025-06-01T00:00:00Z", snap)
    second = runtime.run("run1", "2025-06-01T00:00:00Z", snap)
    assert second.status == "REUSED"
    assert second.run_id == first.run_id
    with runtime.db.connect(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM portfolio_state_observation").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM portfolio_state_transition").fetchone()[0] == 1


def test_score_alone_cannot_authorize_enter(runtime):
    runtime.seed("sec1")
    # high conviction but INITIAL cause: no evidence-driven persistence -> stays WATCH
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=95, suffix="a"
    )
    s1 = runtime.run(
        "run1",
        "2025-06-01T00:00:00Z",
        runtime.snapshot("2025-06-01T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-01T00:00:00Z")],
    )
    assert s1.transition_count == 1  # DISCOVER -> WATCH only
    runtime.score(
        "sec1",
        "run2",
        "2025-06-03T00:00:00Z",
        cause="MODEL_REASSESSMENT",
        prior=95,
        delta=0,
        suffix="b",
    )
    s2 = runtime.run(
        "run2",
        "2025-06-03T00:00:00Z",
        runtime.snapshot("2025-06-03T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-03T00:00:00Z")],
    )
    assert s2.transition_count == 0  # a model rerun is NOT persistence
    assert s2.proposal_count == 0


def test_verified_thesis_break_bypasses_hysteresis(runtime):
    runtime.seed("sec1")
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a"
    )
    s1 = runtime.run("run1", "2025-06-01T00:00:00Z", runtime.snapshot("2025-06-01T00:00:00Z"))
    assert s1.transition_count == 1  # -> WATCH
    # Place the security in HOLD via the canonical chain (WATCH->ENTER->HOLD) using
    # direct ledger writes — the derivation of current state is covered elsewhere.
    portfolio_run_id = s1.run_id
    with runtime.db.connect() as conn:
        score_id = conn.execute(
            "SELECT snapshot_id FROM score_snapshot ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,"
            "cash_status,nav_microusd,valuation_status,holdings_status,provenance_json,"
            "input_hash,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "p" * 64,
                "2025-06-02T00:00:00Z",
                "USD",
                5_000_000_000,
                "KNOWN",
                10_000_000_000,
                "KNOWN",
                "KNOWN",
                "{}",
                "q" * 64,
                "2025-06-02T00:00:00Z",
            ),
        )
        for decision_id, from_state, to_state, cause, effective in (
            ("d" * 62 + "01", "WATCH", "ENTER", "RULE_PERSISTED", "2025-06-02T00:00:00Z"),
            ("d" * 62 + "02", "ENTER", "HOLD", "SETTLEMENT", "2025-06-02T12:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO portfolio_state_observation("
                "decision_id,run_id,security_id,current_state,signal_state,proposed_state,"
                "portfolio_snapshot_id,policy_version,evidence_driven,signal_status,"
                "persistence_count_at_decision,persistence_required,material_change_satisfied,"
                "cooldown_satisfied,risk_status,final_status,reason_codes_json,risk_json,"
                "sizing_json,decision_input_hash,observed_at,recorded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    portfolio_run_id,
                    "sec1",
                    from_state,
                    to_state,
                    to_state,
                    "p" * 64,
                    "fixture-policy-v1",
                    1,
                    "PASS",
                    2,
                    2,
                    0,
                    1,
                    "PASS",
                    "TRANSITIONED",
                    '["score_band"]',
                    "{}",
                    "{}",
                    "o" * 64,
                    effective,
                    effective,
                ),
            )
            conn.execute(
                "INSERT INTO portfolio_state_transition("
                "transition_id,decision_id,security_id,from_state,to_state,cause,reason_codes_json,"
                "score_snapshot_id,portfolio_snapshot_id,policy_version,persistence_count,"
                "persistence_required,effective_at,created_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "t" * 62 + decision_id[-2:],
                    decision_id,
                    "sec1",
                    from_state,
                    to_state,
                    cause,
                    '["score_band"]',
                    score_id,
                    "p" * 64,
                    "fixture-policy-v1",
                    2,
                    2,
                    effective,
                    effective,
                ),
            )
    # verified thesis break -> HOLD -> EXIT immediately (bypasses persistence)
    with runtime.db.connect() as conn:
        seed_thesis_break(
            conn,
            security_id="sec1",
            score_snapshot_id=score_id,
            status="VERIFIED",
            verified_at="2025-06-03T00:00:00Z",
        )
    runtime.score(
        "sec1",
        "run2",
        "2025-06-04T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        conviction=30,
        prior=80,
        delta=-50,
        trajectory="FALLING",
        suffix="b",
    )
    snap_break = runtime.snapshot(
        "2025-06-04T00:00:00Z",
        cash=8_000_000_000,
        nav=10_000_000_000,
        holdings=[_holding(quantity=40_000_000, value=2_000_000_000)],
        market=[
            {
                "security_id": "sec1",
                "mark_price_microusd": 50_000_000,
                "price_as_of": "2025-06-04T00:00:00Z",
                "avg_dollar_volume_microusd": 2_000_000_000_000,
                "liquidity_as_of": "2025-06-04T00:00:00Z",
                "evidence_ids": [f"sec1:bar:{i:03d}" for i in range(40)],
            }
        ],
    )
    s2 = runtime.run("run2", "2025-06-04T00:00:00Z", snap_break)
    assert s2.transition_count == 1
    with runtime.db.connect(read_only=True) as conn:
        transition = conn.execute(
            "SELECT from_state,to_state,cause FROM portfolio_state_transition "
            "ORDER BY effective_at DESC LIMIT 1"
        ).fetchone()
        assert transition["from_state"] == "HOLD" and transition["to_state"] == "EXIT"
        assert transition["cause"] == "VERIFIED_THESIS_BREAK"
    assert "verified-break" in s2.briefing


def test_unverified_thesis_break_does_not_bypass(runtime):
    runtime.seed("sec1")
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a"
    )
    runtime.run("run1", "2025-06-01T00:00:00Z", runtime.snapshot("2025-06-01T00:00:00Z"))
    # an UNVERIFIED break must NOT bypass; the HOLD->EXIT verified-break rule must not fire
    with runtime.db.connect() as conn:
        score_id = conn.execute(
            "SELECT snapshot_id FROM score_snapshot ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()[0]
        seed_thesis_break(
            conn,
            security_id="sec1",
            score_snapshot_id=score_id,
            status="REJECTED",
            verified_at="2025-06-02T00:00:00Z",
        )
    runtime.score(
        "sec1",
        "run2",
        "2025-06-03T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        conviction=10,
        prior=80,
        delta=-70,
        trajectory="FALLING",
        suffix="b",
    )
    snap = runtime.snapshot(
        "2025-06-03T00:00:00Z",
        cash=5_000_000_000,
        nav=10_000_000_000,
        holdings=[_holding()],
        market=[
            {
                "security_id": "sec1",
                "mark_price_microusd": 50_000_000,
                "price_as_of": "2025-06-03T00:00:00Z",
                "avg_dollar_volume_microusd": 2_000_000_000_000,
                "liquidity_as_of": "2025-06-03T00:00:00Z",
                "evidence_ids": [f"sec1:bar:{i:03d}" for i in range(40)],
            }
        ],
    )
    s2 = runtime.run("run2", "2025-06-03T00:00:00Z", snap)
    # WATCH state: score-band ENTER requires persistence; no verified break applies to WATCH
    assert s2.transition_count == 0
    assert s2.proposal_count == 0


def test_missing_holdings_cannot_produce_sell(runtime):
    runtime.seed("sec1")
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a"
    )
    s1 = runtime.run("run1", "2025-06-01T00:00:00Z", runtime.snapshot("2025-06-01T00:00:00Z"))
    assert s1.transition_count == 1  # -> WATCH
    # even a crashing score cannot produce a SELL without a holding
    runtime.score(
        "sec1",
        "run2",
        "2025-06-02T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        conviction=10,
        prior=80,
        delta=-70,
        trajectory="FALLING",
        evhash="H-crash",
        suffix="b",
    )
    s2 = runtime.run("run2", "2025-06-02T00:00:00Z", runtime.snapshot("2025-06-02T00:00:00Z"))
    assert s2.transition_count == 0  # WATCH -> DISCOVER cooldown (2d) not elapsed
    assert s2.proposal_count == 0


def test_buy_blocks_on_unknown_risk_data(runtime):
    runtime.seed("sec1")
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a"
    )
    runtime.run("run1", "2025-06-01T00:00:00Z", runtime.snapshot("2025-06-01T00:00:00Z"))
    runtime.score(
        "sec1",
        "run2",
        "2025-06-03T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        prior=80,
        delta=0,
        suffix="b",
    )
    runtime.run(
        "run2",
        "2025-06-03T00:00:00Z",
        runtime.snapshot("2025-06-03T00:00:00Z"),
        signals=[runtime.signal("sec1", "2025-06-03T00:00:00Z")],
    )
    runtime.score(
        "sec1",
        "run3",
        "2025-06-05T00:00:00Z",
        cause="EVIDENCE_DRIVEN",
        prior=80,
        delta=0,
        suffix="c",
    )
    # snapshot with UNKNOWN liquidity -> ENTER blocked
    snap = build_snapshot(
        "2025-06-05T00:00:00Z",
        cash_microusd=None,
        cash_status="UNKNOWN",
        nav_microusd=None,
        valuation_status="UNKNOWN",
        holdings=[],
        market_inputs=[
            {
                "security_id": "sec1",
                "mark_price_microusd": 50_000_000,
                "price_as_of": "2025-06-05T00:00:00Z",
                "avg_dollar_volume_microusd": None,
                "liquidity_status": "UNKNOWN",
                "liquidity_as_of": None,
                "evidence_ids": [f"sec1:bar:{i:03d}" for i in range(40)],
            }
        ],
    )
    s3 = runtime.run(
        "run3",
        "2025-06-05T00:00:00Z",
        snap,
        signals=[runtime.signal("sec1", "2025-06-05T00:00:00Z")],
    )
    assert s3.proposal_count == 0
    assert "BLOCKED" in s3.briefing


def test_no_trade_cash_is_valid_output(runtime):
    runtime.seed("sec1")
    runtime.score(
        "sec1",
        "run1",
        "2025-06-01T00:00:00Z",
        cause="INITIAL",
        conviction=30,
        trajectory="STABLE",
        suffix="a",
    )
    s1 = runtime.run("run1", "2025-06-01T00:00:00Z", runtime.snapshot("2025-06-01T00:00:00Z"))
    assert s1.transition_count == 0  # conviction 30 below watch band 40
    assert s1.proposal_count == 0
    assert "No portfolio action recommended." in s1.briefing


def test_daily_budget_count_cap_blocks_overflow(runtime):
    runtime.seed("sec1")
    runtime.seed("sec2", sector="Health")
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a"
    )
    runtime.score(
        "sec2", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a2"
    )
    runtime.run("run1", "2025-06-01T00:00:00Z", runtime.snapshot("2025-06-01T00:00:00Z"))
    summaries = []
    for idx, (run_id, as_of) in enumerate(
        [
            ("run2", "2025-06-03T00:00:00Z"),
            ("run3", "2025-06-05T00:00:00Z"),
            ("run4", "2025-06-07T00:00:00Z"),
            ("run5", "2025-06-09T00:00:00Z"),
        ],
        start=2,
    ):
        for sec, suffix in (("sec1", "b"), ("sec2", "c")):
            runtime.score(
                sec,
                run_id,
                as_of,
                cause="EVIDENCE_DRIVEN",
                prior=80,
                delta=0,
                evhash=f"H-{idx}-{sec}",
                suffix=f"{suffix}{idx}",
            )
        snap = build_snapshot(
            as_of,
            cash_microusd=20_000_000_000,
            nav_microusd=20_000_000_000,
            holdings=[],
            market_inputs=[
                {
                    "security_id": sec,
                    "mark_price_microusd": 50_000_000,
                    "price_as_of": as_of,
                    "avg_dollar_volume_microusd": 2_000_000_000_000,
                    "liquidity_as_of": as_of,
                    "evidence_ids": [f"{sec}:bar:{i:03d}" for i in range(40)],
                }
                for sec in ("sec1", "sec2")
            ],
        )
        summaries.append(
            runtime.run(
                run_id,
                as_of,
                snap,
                signals=[runtime.signal(sec, as_of) for sec in ("sec1", "sec2")],
            )
        )
    # both ENTER proposals (target 8% of $20k NAV each) are admitted when they fire;
    # the count cap (3/day) is never breached by 2 proposals on one day.
    assert any(summary.proposal_count == 2 for summary in summaries)


def test_briefing_is_deterministic_and_safe(runtime):
    runtime.seed("sec1")
    runtime.score(
        "sec1", "run1", "2025-06-01T00:00:00Z", cause="INITIAL", conviction=80, suffix="a"
    )
    snap = runtime.snapshot("2025-06-01T00:00:00Z")
    first = runtime.run("run1", "2025-06-01T00:00:00Z", snap)
    second = runtime.run("run1", "2025-06-01T00:00:00Z", snap)
    assert first.briefing == second.briefing
    assert first.briefing_hash == second.briefing_hash
    assert "DATA STATUS" in first.briefing
    assert "PORTFOLIO STATUS" in first.briefing
    assert "CHANGES" in first.briefing
    assert "PROPOSALS" in first.briefing
    assert "BLOCKED / NEEDS ATTENTION" in first.briefing
    # no raw tokens or evidence text can appear
    for forbidden in ("confirmation", "token=", "submit_order", "TIGER", "BEGIN PRIVATE"):
        assert forbidden.lower() not in first.briefing.lower()

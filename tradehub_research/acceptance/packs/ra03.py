"""RA-03: deterministic qualification of the V2 Phase 3 portfolio plane.

Covers: state-machine contract, persistence/hysteresis, thesis-break bypass,
long-only SELL, risk checks, daily aggregate budget, proposal contract,
deterministic briefing, and the no-execution-leakage boundary.

Each assertion builds its own temporary database and raises AssertionError on
failure; the whitelisted runner supplies PASS/FAIL/BLOCKED/ESCALATE.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

from tradehub_research.db import ResearchDB
from tradehub_research.portfolio.budget import Budget, BudgetState, admit_drafts
from tradehub_research.portfolio.engine import PortfolioEngine
from tradehub_research.portfolio.fixtures import fixture_policy, fixture_policy_spec
from tradehub_research.portfolio.policy import (
    PolicyRegistry,
    build_policy,
    load_policy_from_json,
)
from tradehub_research.portfolio.prices import (
    average_dollar_volume,
    sample_volatility,
    total_return_series,
)
from tradehub_research.portfolio.proposal import ProposalError, build_proposal
from tradehub_research.portfolio.risk import RiskEngine, RiskInputs
from tradehub_research.portfolio.sizing import size_buy, size_sell
from tradehub_research.portfolio.snapshot import build_signal_input, build_snapshot
from tradehub_research.portfolio.state import (
    cooldown_satisfied,
    current_state,
)
from tradehub_research.portfolio.types import (
    Action,
    C,
    D,
    PolicyStatus,
    State,
    json_roundtrip,
)
from tradehub_research.schema import PHASE_0_SCHEMA_VERSION
from tradehub_research.screens import canonical_json

# ruff: noqa: E501 -- SQL projection strings mirror immutable row layouts.


class _ExpectError:
    """Standalone pytest.raises equivalent — packs must run in bare venvs."""

    def __init__(self, exc_type, match=None):
        self.exc_type = exc_type
        self.match = match

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            raise AssertionError(f"expected {self.exc_type.__name__} but nothing was raised")
        if not issubclass(exc_type, self.exc_type):
            raise AssertionError(
                f"expected {self.exc_type.__name__}, got {exc_type.__name__}: {exc_value}"
            )
        if self.match is not None and self.match not in str(exc_value):
            raise AssertionError(f"expected {self.match!r} in {exc_value!r}")
        return True


def _raises(exc_type, match=None):
    return _ExpectError(exc_type, match)


def _seed_security(
    db,
    security_id: str,
    *,
    sector: str = "Tech",
    coverage: str = "SUPPORTED",
    delisted_at: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
        (
            security_id,
            security_id.upper(),
            "NYSE",
            f"{security_id} Inc",
            sector,
            None,
            coverage,
            "2024-01-01",
            delisted_at,
        ),
    )


def _seed_pipeline_run(db, run_id: str, as_of: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,"
        "screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,"
        "input_view_hash,expected_security_count,status,failure_json,started_at,finished_at,"
        "flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            as_of,
            "universe",
            "[]",
            "manifest",
            "{}",
            "funnel",
            None,
            "view",
            1,
            "COMPLETE",
            None,
            as_of,
            as_of,
            "[]",
        ),
    )


def _seed_evidence(
    db,
    security_id: str,
    *,
    record_type: str,
    session_date: str,
    pat: str,
    evidence_id: str,
    fields: dict,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO evidence_source VALUES (?,?,?,?,?)",
        ("tiingo_eod", "market_data", 1, None, "derived_from_index"),
    )
    db.execute(
        "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            security_id,
            "tiingo_eod",
            canonical_json(
                {"record_type": record_type, "provider_ticker": security_id.upper(), **fields}
            ),
            1.0,
            None,
            0,
            "hash",
            f"{security_id}:{session_date}:{record_type}",
            session_date,
            pat,
            "derived_from_index",
            pat,
        ),
    )


def _seed_bars(
    db,
    security_id: str,
    closes: list[float],
    *,
    start: str = "2025-01-02",
    volumes: list[int] | None = None,
) -> None:
    volumes = volumes or [1_000_000] * len(closes)
    session = date.fromisoformat(start)
    for index, (close, volume) in enumerate(zip(closes, volumes, strict=False)):
        session_text = session.isoformat()
        _seed_evidence(
            db,
            security_id,
            record_type="price_bar",
            session_date=session_text,
            pat=f"{session_text}T21:00:00Z",
            evidence_id=f"{security_id}:bar:{index:03d}",
            fields={
                "session_date": session_text,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
            },
        )
        session += timedelta(days=1)


def _seed_score(
    db,
    *,
    pipeline_run_id: str,
    security_id: str,
    conviction: int = 80,
    data_quality: float = 0.9,
    agreement: float = 0.8,
    trajectory: str = "RISING",
    cause: str = "INITIAL",
    evhash: str | None = None,
    material_time: str | None = None,
    prior: int | None = None,
    delta: int | None = None,
    suffix: str = "a",
    run_as_of: str | None = None,
) -> str:
    run_as_of = run_as_of or "2025-06-01T00:00:00Z"
    evidence_hash = evhash or f"ev-{security_id}-{suffix}"
    candidate_id = f"cand-{security_id}-{suffix}"
    committee_run_id = f"cr-{security_id}-{suffix}"
    comparison_id = f"cmp-{security_id}-{suffix}"
    scoring_config_hash = f"sc-{security_id}-{suffix}"
    comparator_config_hash = f"cc-{security_id}-{suffix}"
    pack_hash = f"pack-{security_id}-{suffix}"
    assessment_a = f"as-a-{security_id}-{suffix}"
    assessment_b = f"as-b-{security_id}-{suffix}"
    screen_definition_hash = f"sd-{security_id}-{suffix}"
    version_number = (
        1 + int(hashlib.sha256(f"{security_id}{suffix}".encode()).hexdigest()[:8], 16) % 100000
    )
    ordinal = (
        1 + int(hashlib.sha256(f"{security_id}{suffix}ord".encode()).hexdigest()[:8], 16) % 1000
    )
    db.execute(
        "INSERT INTO scoring_version(config_hash,scoring_version,spec_json,description,created_at)"
        " VALUES (?,?,?,?,?)",
        (scoring_config_hash, version_number, '{"v":1}', "fixture", "2025-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO comparator_definition(config_hash,comparator_version,taxonomy_version,"
        "spec_json,created_at) VALUES (?,?,?,?,?)",
        (comparator_config_hash, version_number, 1, '{"v":1}', "2025-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT OR IGNORE INTO screen_definition VALUES (?,?,?,?,?,?)",
        (screen_definition_hash, "valuation", "value", 1, "{}", "2025-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO candidate(candidate_id,run_id,security_id,ordinal,inclusion_reasons_json,"
        "screen_result_ids_json,rank_telemetry_json,is_control,included_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            candidate_id,
            pipeline_run_id,
            security_id,
            ordinal,
            "[]",
            json_roundtrip([screen_definition_hash]),
            "{}",
            0,
            run_as_of,
        ),
    )
    db.execute(
        "INSERT INTO evidence_pack(pack_hash,pack_spec_version,candidate_id,pipeline_run_id,"
        "body_json,body_chars,built_at) VALUES (?,?,?,?,?,?,?)",
        (
            pack_hash,
            1,
            candidate_id,
            pipeline_run_id,
            json_roundtrip(
                {
                    "run": {"as_of": run_as_of},
                    "evidence": [{"evidence_id": f"{security_id}:e:{suffix}"}],
                    "screens": [
                        {
                            "family": "valuation",
                            "screen_id": "value",
                            "version": 1,
                            "passed": True,
                            "evidence_ids": [f"{security_id}:e:{suffix}"],
                            "raw_features": {},
                        }
                    ],
                }
            ),
            100,
            run_as_of,
        ),
    )
    db.execute(
        "INSERT INTO committee_run(committee_run_id,candidate_id,pipeline_run_id,pack_hash,"
        "role_set_json,committee_policy_version,comparator_config_hash,scoring_config_hash,"
        "prompt_versions_json,assessment_schema_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            committee_run_id,
            candidate_id,
            pipeline_run_id,
            pack_hash,
            '["neutral_analyst_a","neutral_analyst_b"]',
            1,
            comparator_config_hash,
            scoring_config_hash,
            '{"neutral":"v1"}',
            1,
            run_as_of,
        ),
    )
    for role, assessment_id, provider in (
        ("neutral_analyst_a", assessment_a, "provider-a"),
        ("neutral_analyst_b", assessment_b, "provider-b"),
    ):
        db.execute(
            "INSERT INTO model_assessment(assessment_id,committee_run_id,candidate_id,pack_hash,"
            "role,provider,model_id,prompt_version,assessment_schema_version,taxonomy_version,"
            "model_route,billing_class,claims_json,cited_evidence_ids_json,missing_evidence_json,"
            "thesis_json,confidence,uncertainty,usage_json,cost_json,evaluation_time,submitted_at,"
            "payload_hash,semantic_assessment_hash) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                assessment_id,
                committee_run_id,
                candidate_id,
                pack_hash,
                role,
                provider,
                "model",
                "v1",
                1,
                1,
                "route",
                "local",
                "[]",
                json_roundtrip([f"{security_id}:e:{suffix}"]),
                "[]",
                '{"summary":"s","upside_mechanism":"u","downside_mechanism":"d",'
                '"thesis_break_conditions":[]}',
                0.5,
                0.5,
                '{"input_tokens":null,"output_tokens":null,"cached_tokens":null,"source":"UNKNOWN"}',
                '{"amount":null,"currency":null,"source":"UNKNOWN"}',
                run_as_of,
                run_as_of,
                "payload",
                f"semantic-{role}",
            ),
        )
    db.execute(
        "INSERT INTO comparison_report(comparison_id,committee_run_id,assessment_id_a,"
        "assessment_id_b,comparator_config_hash,report_json,agreement,routing_decision,"
        "result_hash,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            comparison_id,
            committee_run_id,
            assessment_a,
            assessment_b,
            comparator_config_hash,
            "{}",
            agreement,
            "SCORE",
            "result",
            run_as_of,
        ),
    )
    score_input_hash = C(
        {
            "scoring_config_hash": scoring_config_hash,
            "candidate_id": candidate_id,
            "security_id": security_id,
            "scored_evidence_hash": evidence_hash,
        }
    )
    snapshot_id = D("score-snapshot-v1", score_input_hash)
    delta_value = (
        delta if delta is not None else (conviction - prior if prior is not None else None)
    )
    db.execute(
        "INSERT INTO score_snapshot(snapshot_id,candidate_id,committee_run_id,scoring_config_hash,"
        "score_input_hash,scored_evidence_hash,assessment_ids_json,comparison_id,"
        "resolution_ids_json,family_contributions_json,underlying_groups_json,penalties_json,"
        "base_evidence,confluence_bonus,raw_score,conviction,data_quality,committee_agreement,"
        "prior_snapshot_id,prior_conviction,conviction_delta,trajectory_label,change_cause,"
        "material_change_time,reason_codes_json,result_hash,computed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id,
            candidate_id,
            committee_run_id,
            scoring_config_hash,
            score_input_hash,
            evidence_hash,
            json_roundtrip([assessment_a, assessment_b]),
            comparison_id,
            "[]",
            "{}",
            "{}",
            "{}",
            0.5,
            0.1,
            0.6,
            conviction,
            data_quality,
            agreement,
            None,
            prior,
            delta_value,
            trajectory,
            cause,
            material_time,
            "[]",
            "result",
            run_as_of,
        ),
    )
    return snapshot_id


def _seed_thesis_break(
    db,
    *,
    security_id: str,
    score_snapshot_id: str,
    status: str = "VERIFIED",
    method: str = "FIXTURE",
    verified_at: str = "2025-06-02T00:00:00Z",
) -> None:
    event_material = {
        "security_id": security_id,
        "condition_id": "cond-1",
        "condition_text": "fixture",
        "evidence_ids": ["e-break"],
        "detection_score_snapshot_id": score_snapshot_id,
        "detected_at": verified_at,
    }
    event_id = D("thesis-break-v1", C(event_material))
    db.execute(
        "INSERT INTO thesis_break_event(event_id,security_id,condition_id,condition_text,"
        "evidence_ids_json,detection_score_snapshot_id,detected_at,input_hash,recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            security_id,
            "cond-1",
            "fixture",
            json_roundtrip(["e-break"]),
            score_snapshot_id,
            verified_at,
            C(event_material),
            verified_at,
        ),
    )
    verification_material = {
        "event_id": event_id,
        "status": status,
        "verification_method": method,
        "verified_at": verified_at,
        "score_snapshot_id": score_snapshot_id,
        "evidence_ids": ["e-break"],
        "verifier_ref": "ra03",
    }
    verification_id = D("thesis-verification-v1", C(verification_material))
    db.execute(
        "INSERT INTO thesis_break_verification(verification_id,event_id,status,verification_method,"
        "verified_at,score_snapshot_id,evidence_ids_json,verifier_ref,input_hash,recorded_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            verification_id,
            event_id,
            status,
            method,
            verified_at,
            score_snapshot_id,
            json_roundtrip(["e-break"]),
            "ra03",
            C(verification_material),
            verified_at,
        ),
    )


def _runtime(tmp: Path) -> ResearchDB:
    database = ResearchDB(tmp / f"research-{uuid.uuid4().hex[:8]}.db")
    database.migrate()
    PolicyRegistry(database).register(fixture_policy())
    return database


def _market_input(security_id: str, as_of: str) -> dict:
    return {
        "security_id": security_id,
        "mark_price_microusd": 50_000_000,
        "price_as_of": as_of,
        "avg_dollar_volume_microusd": 2_000_000_000_000,
        "liquidity_as_of": as_of,
        "evidence_ids": [f"{security_id}:bar:{i:03d}" for i in range(40)],
    }


def _empty_snapshot(
    as_of: str,
    *,
    cash: int = 10_000_000_000,
    nav: int = 10_000_000_000,
    holdings: list | None = None,
    market: list | None = None,
):
    return build_snapshot(
        as_of,
        cash_microusd=cash,
        nav_microusd=nav,
        holdings=holdings or [],
        market_inputs=market or [],
    )


def _closes(n: int = 40) -> list[float]:
    return [50.0 + (i % 7) * 0.5 for i in range(n)]


# ---------------------------------------------------------------------------
# 1. migration/store
# ---------------------------------------------------------------------------


def ra03_01_migration_and_append_only(tmp: Path) -> None:
    database = _runtime(tmp)
    assert database.schema_version() == PHASE_0_SCHEMA_VERSION == 10
    with database.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "portfolio_policy",
            "portfolio_snapshot",
            "portfolio_state_transition",
            "portfolio_state_observation",
            "trade_proposal",
            "portfolio_activity_day",
            "portfolio_briefing",
            "thesis_break_event",
            "thesis_break_verification",
        }
        assert required <= tables, f"missing tables: {required - tables}"
        # every portfolio-plane table carries append-only UPDATE/DELETE triggers
        triggers = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for table in sorted(required):
            assert f"{table}_no_update" in triggers, f"missing {table}_no_update"
            assert f"{table}_no_delete" in triggers, f"missing {table}_no_delete"
        # row-level aborts prove the triggers are live
        _seed_security(db, "sec1")
        db.execute(
            "INSERT INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,cash_status,"
            "nav_microusd,valuation_status,holdings_status,provenance_json,input_hash,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s" * 64,
                "2025-06-01T00:00:00Z",
                "USD",
                10_000_000_000,
                "KNOWN",
                10_000_000_000,
                "KNOWN",
                "KNOWN",
                "{}",
                "i" * 64,
                "2025-06-01T00:00:00Z",
            ),
        )
        for statement in (
            "UPDATE portfolio_snapshot SET as_of='2025-06-02T00:00:00Z'",
            "DELETE FROM portfolio_snapshot",
        ):
            with _ExpectSqliteError("append-only"):
                db.execute(statement)
        # the edge CHECK is encoded in the DDL (invalid transitions rejected)
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='portfolio_state_transition'"
        ).fetchone()[0]
        assert "from_state" in ddl and "to_state" in ddl and "CHECK" in ddl


class _ExpectSqliteError:
    def __init__(self, fragment: str):
        self.fragment = fragment

    def __enter__(self) -> _ExpectSqliteError:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        assert exc_type is sqlite3.IntegrityError, f"expected IntegrityError, got {exc_type}"
        assert self.fragment.lower() in str(exc_value).lower(), f"unexpected message: {exc_value}"
        return True


# ---------------------------------------------------------------------------
# 2. policy registry
# ---------------------------------------------------------------------------


def ra03_02_policy_hash_idempotence_and_collision(tmp: Path) -> None:
    database = _runtime(tmp)
    registry = PolicyRegistry(database)
    policy = fixture_policy()
    registry.register(policy)
    registry.register(policy)  # idempotent
    loaded = registry.get(policy.policy_version)
    assert loaded.spec_hash == policy.spec_hash
    assert loaded.spec_json == policy.spec_json
    modified = fixture_policy_spec()
    modified["budget"]["max_actionable_count"] = 7
    # same version, different content: rejected
    with _raises(ValueError):
        registry.register(build_policy(policy.policy_version, PolicyStatus.FIXTURE, modified))
    # version collision on identical content is fine; different version with modified
    # content registers cleanly
    registry.register(build_policy("other-version", PolicyStatus.FIXTURE, modified))


# ---------------------------------------------------------------------------
# 3. fail closed
# ---------------------------------------------------------------------------


def ra03_03_policy_fail_closed(tmp: Path) -> None:
    database = _runtime(tmp)
    engine = PortfolioEngine(database)
    snapshot = _empty_snapshot("2025-06-01T00:00:00Z")
    # unknown version
    with _raises(KeyError):
        engine.run(
            pipeline_run_id="run1",
            policy_version="missing",
            snapshot=snapshot,
            decision_as_of="2025-06-01T00:00:00Z",
        )
    # FIXTURE rejected without allow_fixture
    with _raises(ValueError, match="FIXTURE"):
        engine.run(
            pipeline_run_id="run1",
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of="2025-06-01T00:00:00Z",
        )
    # PROVISIONAL requires opt-in
    provisional_spec = fixture_policy_spec()
    provisional_spec["budget"]["max_actionable_count"] = 4
    provisional = build_policy("provisional-v1", PolicyStatus.PROVISIONAL, provisional_spec)
    PolicyRegistry(database).register(provisional)
    with _raises(ValueError, match="allow-provisional"):
        engine.run(
            pipeline_run_id="run1",
            policy_version="provisional-v1",
            snapshot=snapshot,
            decision_as_of="2025-06-01T00:00:00Z",
        )
    # malformed spec never registers
    with _raises(ValueError):
        load_policy_from_json("bad-v1", PolicyStatus.FIXTURE, '{"not":"a policy"}')


# ---------------------------------------------------------------------------
# 4. snapshot typing
# ---------------------------------------------------------------------------


def ra03_04_snapshot_contract(tmp: Path) -> None:
    a = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=10_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[],
    )
    b = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=10_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[],
    )
    assert a.snapshot_id == b.snapshot_id  # canonical replay identity
    unknown = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=None,
        cash_status="UNKNOWN",
        nav_microusd=None,
        valuation_status="UNKNOWN",
        holdings_status="UNKNOWN",
        holdings=[],
    )
    assert unknown.snapshot_id != a.snapshot_id  # empty-known != unknown
    with _raises(ValueError, match="NAV mismatch"):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=5_000_000_000,
            nav_microusd=10_000_000_000,
            holdings=[],
        )
    with _raises(ValueError):
        build_snapshot(
            "2025-06-01T00:00:00Z",
            cash_microusd=10_000_000_000,
            nav_microusd=10_000_000_000,
            market_inputs=[
                _market_input("sec1", "2025-06-01T00:00:00Z"),
                _market_input("sec1", "2025-06-01T00:00:00Z"),
            ],
        )


# ---------------------------------------------------------------------------
# 5. state derivation + edges
# ---------------------------------------------------------------------------


def ra03_05_state_derivation_and_edges(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        state = current_state(conn, "sec1", "2025-06-01T00:00:00Z")
        assert state["state"] == State.DISCOVER  # default
    from tradehub_research.portfolio.types import CANONICAL_EDGES

    assert len(CANONICAL_EDGES) == 11
    expected = {
        ("DISCOVER", "WATCH"),
        ("WATCH", "DISCOVER"),
        ("WATCH", "ENTER"),
        ("ENTER", "HOLD"),
        ("ADD", "HOLD"),
        ("HOLD", "ADD"),
        ("HOLD", "TRIM"),
        ("TRIM", "HOLD"),
        ("TRIM", "EXIT"),
        ("HOLD", "EXIT"),
        ("EXIT", "WATCH"),
    }
    assert {(a.value, b.value) for a, b in CANONICAL_EDGES} == expected


# ---------------------------------------------------------------------------
# 6. idempotent rerun
# ---------------------------------------------------------------------------


def ra03_06_identical_input_idempotent(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        _seed_bars(conn, "sec1", _closes())
        _seed_score(conn, pipeline_run_id="run1", security_id="sec1", suffix="a")
    engine = PortfolioEngine(database)
    snapshot = _empty_snapshot(
        "2025-06-01T00:00:00Z", market=[_market_input("sec1", "2025-06-01T00:00:00Z")]
    )
    first = engine.run(
        pipeline_run_id="run1",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-01T00:00:00Z",
        allow_fixture=True,
    )
    second = engine.run(
        pipeline_run_id="run1",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-01T00:00:00Z",
        allow_fixture=True,
    )
    assert second.status == "REUSED"
    assert second.run_id == first.run_id
    with database.connect(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM portfolio_state_observation").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM portfolio_state_transition").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 7-8. persistence/hysteresis
# ---------------------------------------------------------------------------


def _drive_to_watch(database: ResearchDB) -> tuple[PortfolioEngine, str]:
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
    engine = PortfolioEngine(database)
    run_ids: list[str] = []
    for index in range(1, 6):
        run_id = f"run{index}"
        as_of = f"2025-06-{index * 2:02d}T00:00:00Z"
        with database.connect() as conn:
            _seed_pipeline_run(conn, run_id, as_of)
            cause = "INITIAL" if index == 1 else "EVIDENCE_DRIVEN"
            _seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="sec1",
                cause=cause,
                evhash=f"ev{index}",
                prior=80 if index > 1 else None,
                delta=0 if index > 1 else None,
                suffix=f"r{index}",
                run_as_of=as_of,
            )
        snapshot = _empty_snapshot(as_of, market=[_market_input("sec1", as_of)])
        signal = build_signal_input("sec1", as_of, remaining_opportunity_ppm=500000)
        summary = engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            signals=[signal],
            allow_fixture=True,
        )
        run_ids.append(summary.run_id)
    return engine, run_ids[-1]


def ra03_07_persistence_counts_evidence_driven(tmp: Path) -> None:
    database = _runtime(tmp)
    engine, _ = _drive_to_watch(database)
    with database.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT observed_at,persistence_count_at_decision,evidence_driven,final_status "
            "FROM portfolio_state_observation ORDER BY observed_at"
        ).fetchall()
    # runs 2..5 are evidence-driven; persistence reaches 2 at run 3 -> ENTER
    evidence_rows = [row for row in rows if row["evidence_driven"]]
    counts = [row["persistence_count_at_decision"] for row in evidence_rows]
    assert counts[:2] == [1, 2], counts  # 1st evidence obs: 1; 2nd distinct: 2 -> ENTER
    assert any(row["final_status"] == "PROPOSED" for row in rows)
    with database.connect(read_only=True) as conn:
        pairs = [
            row
            for row in conn.execute(
                "SELECT from_state,to_state FROM portfolio_state_transition ORDER BY effective_at"
            )
        ]
        assert ("WATCH", "ENTER") in [(r["from_state"], r["to_state"]) for r in pairs]


def ra03_08_unchanged_evidence_and_rebases_do_not_count(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
    engine = PortfolioEngine(database)
    for index in range(1, 7):
        run_id = f"run{index}"
        as_of = f"2025-06-{index * 2:02d}T00:00:00Z"
        with database.connect() as conn:
            _seed_pipeline_run(conn, run_id, as_of)
            if index == 1:
                cause, evhash = "INITIAL", "e1"
            elif index in (2, 3):
                cause, evhash = "EVIDENCE_DRIVEN", "e2"  # run 3 repeats run 2's evidence
            elif index == 4:
                cause, evhash = "MODEL_REASSESSMENT", "e4"  # rerun is not evidence
            elif index == 5:
                cause, evhash = "SCORING_VERSION_CHANGE", "e5"  # rebase is not evidence
            else:
                cause, evhash = "EVIDENCE_DRIVEN", "e6"
            _seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="sec1",
                cause=cause,
                evhash=evhash,
                prior=80 if index > 1 else None,
                delta=0 if index > 1 else None,
                suffix=f"r{index}",
                run_as_of=as_of,
            )
        snapshot = _empty_snapshot(as_of, market=[_market_input("sec1", as_of)])
        signal = build_signal_input("sec1", as_of, remaining_opportunity_ppm=500000)
        engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            signals=[signal],
            allow_fixture=True,
        )
    with database.connect(read_only=True) as conn:
        rows = conn.execute(
            "SELECT observed_at,evidence_driven,persistence_count_at_decision "
            "FROM portfolio_state_observation ORDER BY observed_at"
        ).fetchall()
    evidence_driven = [row for row in rows if row["evidence_driven"]]
    # run2 (e2) counts 1; run3 (same e2) deduped -> still 1; run4 (MODEL_REASSESSMENT)
    # and run5 (SCORING_VERSION_CHANGE) are rebases: they neither count nor reset;
    # run6 (e6) -> 2
    counts = [row["persistence_count_at_decision"] for row in evidence_driven]
    assert counts == [1, 1, 2], counts
    with database.connect(read_only=True) as conn:
        transitions = conn.execute(
            "SELECT from_state,to_state,effective_at FROM portfolio_state_transition "
            "ORDER BY effective_at"
        ).fetchall()
        # the rebase runs must NOT have transitioned; ENTER happens only at run6
        entered = [t for t in transitions if t["to_state"] == "ENTER"]
        assert len(entered) == 1, transitions
        assert entered[0]["effective_at"] == "2025-06-12T00:00:00Z"


# ---------------------------------------------------------------------------
# 9. material change bypass
# ---------------------------------------------------------------------------


def ra03_09_material_change_bypass_directional(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
    engine = PortfolioEngine(database)
    # run1: WATCH; run2: EVIDENCE_DRIVEN with +30 conviction delta -> material bypass
    for index, (run_id, as_of, cause, delta, evhash) in enumerate(
        [
            ("run1", "2025-06-02T00:00:00Z", "INITIAL", None, "e1"),
            ("run2", "2025-06-04T00:00:00Z", "EVIDENCE_DRIVEN", 300000, "e2"),
        ],
        start=1,
    ):
        with database.connect() as conn:
            _seed_pipeline_run(conn, run_id, as_of)
            _seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="sec1",
                cause=cause,
                evhash=evhash,
                prior=80 if index > 1 else None,
                delta=delta if index > 1 else None,
                material_time=as_of if index > 1 else None,
                suffix=f"r{index}",
                run_as_of=as_of,
            )
        snapshot = _empty_snapshot(as_of, market=[_market_input("sec1", as_of)])
        signal = build_signal_input("sec1", as_of, remaining_opportunity_ppm=500000)
        engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            signals=[signal],
            allow_fixture=True,
        )
    with database.connect(read_only=True) as conn:
        transitions = conn.execute(
            "SELECT from_state,to_state,cause FROM portfolio_state_transition ORDER BY effective_at"
        ).fetchall()
        last = transitions[-1]
        assert last["from_state"] == "WATCH" and last["to_state"] == "ENTER"
        assert last["cause"] == "MATERIAL_CHANGE"
    # wrong direction: DOWN cannot authorize WATCH->ENTER
    database2 = _runtime(tmp / "material-down")
    with database2.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
        _seed_pipeline_run(conn, "run1", "2025-06-02T00:00:00Z")
        _seed_score(
            conn,
            pipeline_run_id="run1",
            security_id="sec1",
            cause="INITIAL",
            evhash="e1",
            suffix="a",
            run_as_of="2025-06-02T00:00:00Z",
        )
        _seed_pipeline_run(conn, "run2", "2025-06-04T00:00:00Z")
        _seed_score(
            conn,
            pipeline_run_id="run2",
            security_id="sec1",
            cause="EVIDENCE_DRIVEN",
            evhash="e2",
            prior=95,
            delta=-15,
            material_time="2025-06-04T00:00:00Z",
            suffix="b",
            run_as_of="2025-06-04T00:00:00Z",
        )
    engine2 = PortfolioEngine(database2)
    for run_id, as_of in (("run1", "2025-06-02T00:00:00Z"), ("run2", "2025-06-04T00:00:00Z")):
        snapshot = _empty_snapshot(as_of, market=[_market_input("sec1", as_of)])
        signal = build_signal_input("sec1", as_of, remaining_opportunity_ppm=500000)
        engine2.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            signals=[signal],
            allow_fixture=True,
        )
    with database2.connect(read_only=True) as conn:
        transitions = conn.execute(
            "SELECT to_state,cause FROM portfolio_state_transition ORDER BY effective_at"
        ).fetchall()
        assert transitions[-1]["to_state"] == "WATCH"  # DOWN delta must not bypass


# ---------------------------------------------------------------------------
# 10. cooldowns
# ---------------------------------------------------------------------------


def ra03_10_cooldown_boundaries(tmp: Path) -> None:
    # inclusive at the exact second
    assert cooldown_satisfied("2025-06-01T00:00:00Z", "2025-06-03T00:00:00Z", 2) is True
    assert cooldown_satisfied("2025-06-01T00:00:00Z", "2025-06-02T23:59:59Z", 2) is False
    # engine-level: WATCH -> DISCOVER gated by the 2-day cooldown
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
    engine = PortfolioEngine(database)
    for index, (run_id, as_of, cause, evhash, conviction) in enumerate(
        [
            ("run1", "2025-06-01T00:00:00Z", "INITIAL", "e1", 80),
            ("run2", "2025-06-02T00:00:00Z", "EVIDENCE_DRIVEN", "e2", 10),  # 1 day later
            ("run3", "2025-06-03T00:00:00Z", "EVIDENCE_DRIVEN", "e3", 10),  # exactly 2 days
        ],
        start=1,
    ):
        with database.connect() as conn:
            _seed_pipeline_run(conn, run_id, as_of)
            _seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="sec1",
                cause=cause,
                conviction=conviction,
                evhash=evhash,
                prior=80 if index > 1 else None,
                delta=(-70) if index > 1 else None,
                trajectory="FALLING" if index > 1 else "RISING",
                suffix=f"r{index}",
                run_as_of=as_of,
            )
        snapshot = _empty_snapshot(as_of, market=[_market_input("sec1", as_of)])
        engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            allow_fixture=True,
        )
    with database.connect(read_only=True) as conn:
        transitions = conn.execute(
            "SELECT from_state,to_state FROM portfolio_state_transition ORDER BY effective_at"
        ).fetchall()
        states = [(row["from_state"], row["to_state"]) for row in transitions]
        # run2 (1 day) blocked by cooldown; run3 (exactly 2 days) allowed
        assert ("WATCH", "DISCOVER") not in states[:-1]
        assert ("WATCH", "DISCOVER") == states[-1]


# ---------------------------------------------------------------------------
# 11. thesis-break verification
# ---------------------------------------------------------------------------


def ra03_11_thesis_break_verified_only(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
        _seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        score_id = _seed_score(conn, pipeline_run_id="run1", security_id="sec1", suffix="a")
        # unverified event must NOT bypass
        _seed_thesis_break(
            conn,
            security_id="sec1",
            score_snapshot_id=score_id,
            status="REJECTED",
            verified_at="2025-06-01T00:00:00Z",
        )
        # place in HOLD via canonical chain
        conn.execute(
            "INSERT INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,cash_status,"
            "nav_microusd,valuation_status,holdings_status,provenance_json,input_hash,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s" * 64,
                "2025-06-02T00:00:00Z",
                "USD",
                8_000_000_000,
                "KNOWN",
                10_000_000_000,
                "KNOWN",
                "KNOWN",
                "{}",
                "i" * 64,
                "2025-06-02T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO portfolio_run(run_id,pipeline_run_id,decision_as_of,portfolio_snapshot_id,"
            "policy_version,score_set_hash,signal_set_hash,candidate_set_hash,invocation_key,"
            "state_prestate_hash,market_data_prestate_hash,budget_prestate_hash,input_hash,"
            "expected_security_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "r" * 64,
                "run1",
                "2025-06-02T00:00:00Z",
                "s" * 64,
                "fixture-policy-v1",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "g" * 64,
                "h" * 64,
                1,
                "2025-06-02T00:00:00Z",
            ),
        )
        for decision_id, from_state, to_state, cause, effective in (
            ("d" * 62 + "01", "WATCH", "ENTER", "RULE_PERSISTED", "2025-06-02T00:00:00Z"),
            ("d" * 62 + "02", "ENTER", "HOLD", "SETTLEMENT", "2025-06-02T12:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO portfolio_state_observation(decision_id,run_id,security_id,"
                "current_state,signal_state,proposed_state,portfolio_snapshot_id,policy_version,"
                "evidence_driven,signal_status,persistence_count_at_decision,"
                "persistence_required,material_change_satisfied,cooldown_satisfied,risk_status,"
                "final_status,reason_codes_json,risk_json,sizing_json,decision_input_hash,"
                "observed_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    "r" * 64,
                    "sec1",
                    from_state,
                    to_state,
                    to_state,
                    "s" * 64,
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
                "INSERT INTO portfolio_state_transition(transition_id,decision_id,security_id,"
                "from_state,to_state,cause,reason_codes_json,score_snapshot_id,"
                "portfolio_snapshot_id,policy_version,persistence_count,persistence_required,"
                "effective_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "t" * 62 + decision_id[-2:],
                    decision_id,
                    "sec1",
                    from_state,
                    to_state,
                    cause,
                    '["score_band"]',
                    score_id,
                    "s" * 64,
                    "fixture-policy-v1",
                    2,
                    2,
                    effective,
                    effective,
                ),
            )
        # now add a VERIFIED break AFTER the rejected one
        _seed_thesis_break(
            conn,
            security_id="sec1",
            score_snapshot_id=score_id,
            status="VERIFIED",
            verified_at="2025-06-03T00:00:00Z",
        )
        _seed_pipeline_run(conn, "run2", "2025-06-04T00:00:00Z")
        _seed_score(
            conn,
            pipeline_run_id="run2",
            security_id="sec1",
            cause="EVIDENCE_DRIVEN",
            evhash="e2",
            prior=80,
            delta=-50,
            trajectory="FALLING",
            suffix="b",
            run_as_of="2025-06-04T00:00:00Z",
        )
    engine = PortfolioEngine(database)
    snapshot = build_snapshot(
        "2025-06-04T00:00:00Z",
        cash_microusd=8_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[
            {
                "security_id": "sec1",
                "quantity_microunits": 40_000_000,
                "sellable_quantity_microunits": 40_000_000,
                "market_value_microusd": 2_000_000_000,
                "sector": "Tech",
            }
        ],
        market_inputs=[_market_input("sec1", "2025-06-04T00:00:00Z")],
    )
    summary = engine.run(
        pipeline_run_id="run2",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-04T00:00:00Z",
        allow_fixture=True,
    )
    with database.connect(read_only=True) as conn:
        last = conn.execute(
            "SELECT from_state,to_state,cause FROM portfolio_state_transition "
            "ORDER BY effective_at DESC LIMIT 1"
        ).fetchone()
        assert last["from_state"] == "HOLD" and last["to_state"] == "EXIT"
        assert last["cause"] == "VERIFIED_THESIS_BREAK"
    assert summary.proposal_count == 1


# ---------------------------------------------------------------------------
# 12. score alone cannot trade
# ---------------------------------------------------------------------------


def ra03_12_score_alone_cannot_trade(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
        _seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        _seed_score(
            conn,
            pipeline_run_id="run1",
            security_id="sec1",
            conviction=95,
            trajectory="RISING",
            suffix="a",
        )
    engine = PortfolioEngine(database)
    snapshot = _empty_snapshot(
        "2025-06-01T00:00:00Z", market=[_market_input("sec1", "2025-06-01T00:00:00Z")]
    )
    signal = build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=500000)
    summary = engine.run(
        pipeline_run_id="run1",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-01T00:00:00Z",
        signals=[signal],
        allow_fixture=True,
    )
    assert summary.proposal_count == 0  # INITIAL score: no persistence, no trade
    with database.connect(read_only=True) as conn:
        states = {
            row["to_state"]
            for row in conn.execute("SELECT to_state FROM portfolio_state_transition")
        }
        assert states == {"WATCH"}  # watch only; never ENTER on the first score


# ---------------------------------------------------------------------------
# 13. SELL reason whitelist
# ---------------------------------------------------------------------------


def ra03_13_sell_reason_whitelist(tmp: Path) -> None:
    base = dict(
        decision_id="d" * 64,
        transition_id="t" * 64,
        activity_date="2025-06-01",
        security_id="sec1",
        current_state=State.HOLD,
        proposed_state=State.TRIM,
        action=Action.SELL,
        reason_codes=["risk_reduction"],
        conviction_ppm=200000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="FALLING",
        current_weight_ppm=80000,
        target_weight_ppm=40000,
        max_quantity_microunits=1_000_000,
        completion_quantity_microunits=1_000_000,
        max_notional_microusd=100_000_000,
        score_snapshot_id="s" * 64,
        portfolio_snapshot_id="p" * 64,
        policy_version="fixture-policy-v1",
        sizing_policy_version="fixture-sizing-v1",
        quantity_increment_microunits=1_000_000,
        limit_only=True,
        created_at="2025-06-01T00:00:00Z",
    )
    for reason in (
        "thesis_broken",
        "thesis_realised",
        "opportunity_cost",
        "risk_reduction",
        "data_integrity",
        "policy_ineligible",
    ):
        proposal = build_proposal(**{**base, "reason_codes": [reason]})
        assert proposal["action"] == "SELL"
    with _raises(ProposalError, match="invalid SELL reason"):
        build_proposal(**{**base, "reason_codes": ["falling_price"]})
    with _raises(ProposalError, match="invalid SELL reason"):
        build_proposal(**{**base, "reason_codes": ["score_band"]})


# ---------------------------------------------------------------------------
# 14. missing holdings cannot manufacture SELL
# ---------------------------------------------------------------------------


def ra03_14_no_sell_without_holdings(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
        _seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        score_id = _seed_score(conn, pipeline_run_id="run1", security_id="sec1", suffix="a")
        conn.execute(
            "INSERT INTO portfolio_snapshot(snapshot_id,as_of,currency,cash_microusd,cash_status,"
            "nav_microusd,valuation_status,holdings_status,provenance_json,input_hash,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "s" * 64,
                "2025-06-02T00:00:00Z",
                "USD",
                10_000_000_000,
                "KNOWN",
                10_000_000_000,
                "KNOWN",
                "KNOWN",
                "{}",
                "i" * 64,
                "2025-06-02T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO portfolio_run(run_id,pipeline_run_id,decision_as_of,portfolio_snapshot_id,"
            "policy_version,score_set_hash,signal_set_hash,candidate_set_hash,invocation_key,"
            "state_prestate_hash,market_data_prestate_hash,budget_prestate_hash,input_hash,"
            "expected_security_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "r" * 64,
                "run1",
                "2025-06-02T00:00:00Z",
                "s" * 64,
                "fixture-policy-v1",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "g" * 64,
                "h" * 64,
                1,
                "2025-06-02T00:00:00Z",
            ),
        )
        for decision_id, from_state, to_state, cause, effective in (
            ("d" * 62 + "01", "WATCH", "ENTER", "RULE_PERSISTED", "2025-06-02T00:00:00Z"),
            ("d" * 62 + "02", "ENTER", "HOLD", "SETTLEMENT", "2025-06-02T12:00:00Z"),
        ):
            conn.execute(
                "INSERT INTO portfolio_state_observation(decision_id,run_id,security_id,"
                "current_state,signal_state,proposed_state,portfolio_snapshot_id,policy_version,"
                "evidence_driven,signal_status,persistence_count_at_decision,"
                "persistence_required,material_change_satisfied,cooldown_satisfied,risk_status,"
                "final_status,reason_codes_json,risk_json,sizing_json,decision_input_hash,"
                "observed_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    "r" * 64,
                    "sec1",
                    from_state,
                    to_state,
                    to_state,
                    "s" * 64,
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
                "INSERT INTO portfolio_state_transition(transition_id,decision_id,security_id,"
                "from_state,to_state,cause,reason_codes_json,score_snapshot_id,"
                "portfolio_snapshot_id,policy_version,persistence_count,persistence_required,"
                "effective_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "t" * 62 + decision_id[-2:],
                    decision_id,
                    "sec1",
                    from_state,
                    to_state,
                    cause,
                    '["score_band"]',
                    score_id,
                    "s" * 64,
                    "fixture-policy-v1",
                    2,
                    2,
                    effective,
                    effective,
                ),
            )
        _seed_pipeline_run(conn, "run2", "2025-06-03T00:00:00Z")
        _seed_score(
            conn,
            pipeline_run_id="run2",
            security_id="sec1",
            cause="EVIDENCE_DRIVEN",
            evhash="e2",
            prior=80,
            delta=-70,
            trajectory="FALLING",
            suffix="b",
            run_as_of="2025-06-03T00:00:00Z",
        )
    engine = PortfolioEngine(database)
    # HOLD but the snapshot has NO holding (missing/untrusted holdings)
    snapshot = _empty_snapshot(
        "2025-06-03T00:00:00Z", market=[_market_input("sec1", "2025-06-03T00:00:00Z")]
    )
    summary = engine.run(
        pipeline_run_id="run2",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-03T00:00:00Z",
        allow_fixture=True,
    )
    assert summary.proposal_count == 0  # no SELL manufactured from missing holdings


# ---------------------------------------------------------------------------
# 15. concentration blocks
# ---------------------------------------------------------------------------


def ra03_15_concentration_blocks(tmp: Path) -> None:

    # a snapshot with a 60% single-sector position: any ENTER in the same sector
    # is clipped to zero by the sector cap (risk_reduction semantics)
    snapshot = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=4_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[
            {
                "security_id": "sec2",
                "quantity_microunits": 1_000_000,
                "sellable_quantity_microunits": 1_000_000,
                "market_value_microusd": 6_000_000_000,
                "sector": "Tech",
            }
        ],
        market_inputs=[_market_input("sec1", "2025-06-01T00:00:00Z")],
    )
    policy = fixture_policy()
    assert policy.risk["max_position_ppm"] == 100000  # 10%
    holding_weight = round(6_000_000_000 * 1_000_000 / 10_000_000_000)
    assert holding_weight > policy.risk["max_position_ppm"]  # over-concentrated
    # the risk engine blocks a BUY when sector exposure is unknown-critical:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1", sector="Tech")
        _seed_bars(conn, "sec1", _closes())
    engine = RiskEngine(database, policy, snapshot)
    result = engine.evaluate(
        RiskInputs(
            security_id="sec1",
            sector="Tech",
            sector_coverage_status="SUPPORTED",
            current_state=State.WATCH,
            position_present=False,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            mark_price_microusd=50_000_000,
            price_status="KNOWN",
            price_as_of="2025-06-01T00:00:00Z",
            adv_microusd=2_000_000_000_000,
            liquidity_status="KNOWN",
            nav_microusd=10_000_000_000,
            cash_microusd=4_000_000_000,
            current_weight_ppm=0,
            direction=Action.BUY,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "PASS"
    # sector clip: 0 + max(0, 250000 - 600000) = 0 -> target must be 0 -> no BUY
    sized = size_buy(
        policy,
        conviction_ppm=900000,
        data_quality_ppm=900000,
        agreement_ppm=900000,
        trajectory="RISING",
        current_weight_ppm=0,
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips=result.clips,
        available_cash_microusd=4_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    assert sized.action is None  # concentration blocks the trade


# ---------------------------------------------------------------------------
# 16-17. correlation / volatility
# ---------------------------------------------------------------------------


def ra03_16_correlation_blocks(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1", sector="Tech")
        _seed_security(conn, "sec2", sector="Tech")
        closes = [50.0 + i * 0.25 for i in range(60)]
        _seed_bars(conn, "sec1", closes=closes)
        _seed_bars(conn, "sec2", closes=[2 * c for c in closes])  # perfectly correlated
    snapshot = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=8_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[
            {
                "security_id": "sec2",
                "quantity_microunits": 1_000_000,
                "sellable_quantity_microunits": 1_000_000,
                "market_value_microusd": 2_000_000_000,
                "sector": "Tech",
            }
        ],
        market_inputs=[_market_input("sec1", "2025-06-01T00:00:00Z")],
    )
    engine = RiskEngine(database, fixture_policy(), snapshot)
    result = engine.evaluate(
        RiskInputs(
            security_id="sec1",
            sector="Tech",
            sector_coverage_status="SUPPORTED",
            current_state=State.WATCH,
            position_present=False,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            mark_price_microusd=50_000_000,
            price_status="KNOWN",
            price_as_of="2025-06-01T00:00:00Z",
            adv_microusd=2_000_000_000_000,
            liquidity_status="KNOWN",
            nav_microusd=10_000_000_000,
            cash_microusd=8_000_000_000,
            current_weight_ppm=0,
            direction=Action.BUY,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "PASS"
    correlated_book = result.measures.get("correlated_book_ppm", 0)
    assert correlated_book >= 200000
    assert result.clips["correlation"] == 0  # correlated book cap already exceeded
    # insufficient overlap -> correlation UNKNOWN (not silently zero)
    database2 = _runtime(tmp / "overlap")
    with database2.connect() as conn:
        _seed_security(conn, "sec1", sector="Tech")
        _seed_security(conn, "sec2", sector="Tech")
        _seed_bars(conn, "sec1", closes=closes)
        _seed_bars(conn, "sec2", closes=closes, start="2025-04-01")  # no overlap
    snapshot2 = build_snapshot(
        "2025-06-01T00:00:00Z",
        cash_microusd=8_000_000_000,
        nav_microusd=10_000_000_000,
        holdings=[
            {
                "security_id": "sec2",
                "quantity_microunits": 1_000_000,
                "sellable_quantity_microunits": 1_000_000,
                "market_value_microusd": 2_000_000_000,
                "sector": "Tech",
            }
        ],
        market_inputs=[_market_input("sec1", "2025-06-01T00:00:00Z")],
    )
    engine2 = RiskEngine(database2, fixture_policy(), snapshot2)
    result2 = engine2.evaluate(
        RiskInputs(
            security_id="sec1",
            sector="Tech",
            sector_coverage_status="SUPPORTED",
            current_state=State.WATCH,
            position_present=False,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            mark_price_microusd=50_000_000,
            price_status="KNOWN",
            price_as_of="2025-06-01T00:00:00Z",
            adv_microusd=2_000_000_000_000,
            liquidity_status="KNOWN",
            nav_microusd=10_000_000_000,
            cash_microusd=8_000_000_000,
            current_weight_ppm=0,
            direction=Action.BUY,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result2.status == "PASS"
    assert result2.measures.get("correlated_book_ppm", 0) == 0


def ra03_17_volatility_explicit(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", [50.0 + (i % 7) * 0.5 for i in range(40)])
        series = total_return_series(conn, "sec1", "2025-06-01T00:00:00Z")
        assert len(series) == 39
        volatility = sample_volatility(series, 30, 20)
        assert volatility is not None and volatility > 0
    # zero variance -> UNKNOWN
    database2 = _runtime(tmp / "flat")
    with database2.connect() as conn2:
        _seed_security(conn2, "sec2")
        _seed_bars(conn2, "sec2", [50.0] * 40)
        flat_series = total_return_series(conn2, "sec2", "2025-06-01T00:00:00Z")
        assert sample_volatility(flat_series, 30, 20) is None
    # stale price in the snapshot blocks a BUY (evaluated after commit: the
    # ledger must be visible to a fresh connection)
    from tradehub_research.portfolio.risk import RiskEngine, RiskInputs

    engine = RiskEngine(database, fixture_policy(), _empty_snapshot("2025-06-01T00:00:00Z"))
    stale = engine.evaluate(
        RiskInputs(
            security_id="sec1",
            sector="Tech",
            sector_coverage_status="SUPPORTED",
            current_state=State.WATCH,
            position_present=False,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            mark_price_microusd=50_000_000,
            price_status="STALE",
            price_as_of="2025-05-10T00:00:00Z",
            adv_microusd=2_000_000_000_000,
            liquidity_status="KNOWN",
            nav_microusd=10_000_000_000,
            cash_microusd=10_000_000_000,
            current_weight_ppm=0,
            direction=Action.BUY,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert stale.status == "BLOCKED"
    assert "mark_stale" in stale.reasons


# ---------------------------------------------------------------------------
# 18. ADV / liquidity
# ---------------------------------------------------------------------------


def ra03_18_liquidity_clips(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", [50.0] * 20, volumes=[1_000_000] * 20)
        adv = average_dollar_volume(conn, "sec1", "2025-06-01T00:00:00Z", 20, 15)
        assert adv == 50_000_000 * 1_000_000
        # insufficient liquidity history -> UNKNOWN -> BUY blocks
        database2 = _runtime(tmp / "thin")
        with database2.connect() as conn2:
            _seed_security(conn2, "sec2")
            _seed_bars(conn2, "sec2", [50.0] * 3)  # 3 bars < min_adv_observations 15
            assert average_dollar_volume(conn2, "sec2", "2025-06-01T00:00:00Z", 20, 15) is None


# ---------------------------------------------------------------------------
# 19. factor/drawdown seams
# ---------------------------------------------------------------------------


def ra03_19_factor_drawdown_seams(tmp: Path) -> None:
    from tradehub_research.portfolio.policy import build_policy
    from tradehub_research.portfolio.risk import RiskEngine, RiskInputs
    from tradehub_research.portfolio.types import PolicyStatus

    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
    spec = fixture_policy_spec()
    spec["risk"]["factor_required"] = True
    policy = build_policy("factor-required", PolicyStatus.FIXTURE, spec)
    PolicyRegistry(database).register(policy)
    engine = RiskEngine(database, policy, _empty_snapshot("2025-06-01T00:00:00Z"))
    result = engine.evaluate(
        RiskInputs(
            security_id="sec1",
            sector="Tech",
            sector_coverage_status="SUPPORTED",
            current_state=State.WATCH,
            position_present=False,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            mark_price_microusd=50_000_000,
            price_status="KNOWN",
            price_as_of="2025-06-01T00:00:00Z",
            adv_microusd=2_000_000_000_000,
            liquidity_status="KNOWN",
            nav_microusd=10_000_000_000,
            cash_microusd=10_000_000_000,
            current_weight_ppm=0,
            direction=Action.BUY,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result.status == "BLOCKED"
    assert "factor_seam_required_unavailable" in result.reasons
    # not-required seams report NOT_AVAILABLE honestly (no fake exposure)
    engine2 = RiskEngine(database, fixture_policy(), _empty_snapshot("2025-06-01T00:00:00Z"))
    result2 = engine2.evaluate(
        RiskInputs(
            security_id="sec1",
            sector="Tech",
            sector_coverage_status="SUPPORTED",
            current_state=State.WATCH,
            position_present=False,
            trusted_quantity_microunits=None,
            sellable_quantity_microunits=None,
            mark_price_microusd=50_000_000,
            price_status="KNOWN",
            price_as_of="2025-06-01T00:00:00Z",
            adv_microusd=2_000_000_000_000,
            liquidity_status="KNOWN",
            nav_microusd=10_000_000_000,
            cash_microusd=10_000_000_000,
            current_weight_ppm=0,
            direction=Action.BUY,
        ),
        "2025-06-01T00:00:00Z",
    )
    assert result2.status == "PASS"
    assert result2.measures.get("factor_available") is False


# ---------------------------------------------------------------------------
# 20. sizing nonlinearity + cash
# ---------------------------------------------------------------------------


def ra03_20_sizing_nonlinear_and_cash(tmp: Path) -> None:
    policy = fixture_policy()
    kwargs = dict(
        nav_microusd=10_000_000_000,
        mark_price_microusd=50_000_000,
        quantity_increment_microunits=1_000_000,
        clips={
            "position": 100000,
            "sector": 250000,
            "book": 300000,
            "correlation": 150000,
            "volatility": 100000,
            "liquidity": 100000,
        },
        available_cash_microusd=5_000_000_000,
        current_quantity_microunits=0,
        min_action_notional_microusd=1_000_000,
    )
    low = size_buy(
        policy,
        conviction_ppm=690000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        **kwargs,
    )
    high = size_buy(
        policy,
        conviction_ppm=700000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        **kwargs,
    )
    assert low.target_weight_ppm == 50000 and high.target_weight_ppm == 80000
    # zero target: no action
    none = size_buy(
        policy,
        conviction_ppm=300000,
        data_quality_ppm=900000,
        agreement_ppm=800000,
        trajectory="RISING",
        current_weight_ppm=0,
        **kwargs,
    )
    assert none.action is None
    # hold cash: below min notional
    tiny = size_buy(
        policy,
        conviction_ppm=900000,
        data_quality_ppm=900000,
        agreement_ppm=900000,
        trajectory="RISING",
        current_weight_ppm=0,
        available_cash_microusd=100_000,
        **{k: v for k, v in kwargs.items() if k != "available_cash_microusd"},
    )
    assert tiny.action is None
    # SELL bounded by sellable and non-negative completion
    sell = size_sell(
        policy,
        current_weight_ppm=80000,
        current_quantity_microunits=1_000_000,
        sellable_quantity_microunits=400_000,
        mark_price_microusd=50_000_000,
        nav_microusd=10_000_000_000,
        quantity_increment_microunits=1_000_000,
        full_exit=True,
        min_action_notional_microusd=1_000_000,
    )
    assert sell.max_quantity_microunits <= 400_000
    assert sell.completion_quantity_microunits >= 600_000


# ---------------------------------------------------------------------------
# 21-23. daily aggregate budget
# ---------------------------------------------------------------------------


def ra03_21_budget_count_cap_survives_restart(tmp: Path) -> None:
    database = _runtime(tmp)
    state = Budget(database).bind_day("2025-06-01", fixture_policy())
    assert state.max_actionable_count == 3
    # ten tiny drafts: count cap admits exactly 3
    drafts = [
        {
            "security_id": f"sec{i}",
            "max_notional_microusd": 1_000_000,
            "action": "BUY",
            "reason_codes": ["score_band"],
            "category": "score_band",
        }
        for i in range(10)
    ]
    admitted, rejected = admit_drafts(
        state, drafts, fixture_policy(), starting_cash_microusd=10_000_000_000
    )
    assert len(admitted) == 3 and len(rejected) == 7
    # a fresh Budget instance (restart) sees the same day binding
    again = Budget(database).bind_day("2025-06-01", fixture_policy())
    assert again.max_actionable_count == 3


def ra03_22_budget_notional_cap_cannot_be_crossed(tmp: Path) -> None:
    database = _runtime(tmp)
    state = Budget(database).bind_day("2025-06-01", fixture_policy())
    assert state.max_notional_microusd == 5_000_000_000
    drafts = [
        {
            "security_id": "sec1",
            "max_notional_microusd": 3_000_000_000,
            "action": "BUY",
            "reason_codes": ["score_band"],
            "category": "score_band",
        },
        {
            "security_id": "sec2",
            "max_notional_microusd": 3_000_000_000,
            "action": "BUY",
            "reason_codes": ["score_band"],
            "category": "score_band",
        },
    ]
    admitted, rejected = admit_drafts(
        state, drafts, fixture_policy(), starting_cash_microusd=10_000_000_000
    )
    assert len(admitted) == 1
    assert rejected["sec2"] == "daily_budget_exhausted"


def ra03_23_budget_duplicate_consumes_once_and_day_mismatch(tmp: Path) -> None:
    database = _runtime(tmp)
    budget = Budget(database)
    policy = fixture_policy()
    budget.bind_day("2025-06-01", policy)
    # a different policy version on the same day must fail closed
    spec = fixture_policy_spec()
    spec["budget"]["max_actionable_count"] = 1
    other = build_policy("other-v1", PolicyStatus.FIXTURE, spec)
    PolicyRegistry(database).register(other)
    with _raises(ValueError, match="already bound"):
        budget.bind_day("2025-06-01", other)
    # duplicate decision identity consumes budget exactly once: run the same
    # engine invocation twice and assert one proposal row
    engine = PortfolioEngine(database)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
    # drive WATCH -> ENTER (2 evidence-driven observations)
    for index in range(1, 4):
        run_id = f"run{index}"
        as_of = f"2025-06-{index * 2:02d}T00:00:00Z"
        with database.connect() as conn:
            _seed_pipeline_run(conn, run_id, as_of)
            _seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="sec1",
                cause="INITIAL" if index == 1 else "EVIDENCE_DRIVEN",
                evhash=f"e{index}",
                prior=80 if index > 1 else None,
                delta=0 if index > 1 else None,
                suffix=f"r{index}",
                run_as_of=as_of,
            )
        snapshot = _empty_snapshot(as_of, market=[_market_input("sec1", as_of)])
        signal = build_signal_input("sec1", as_of, remaining_opportunity_ppm=500000)
        engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=snapshot,
            decision_as_of=as_of,
            signals=[signal],
            allow_fixture=True,
        )
    # rerun the final invocation: identical input -> REUSED, no duplicate consumption
    final = engine.run(
        pipeline_run_id="run3",
        policy_version="fixture-policy-v1",
        snapshot=_empty_snapshot(
            "2025-06-06T00:00:00Z", market=[_market_input("sec1", "2025-06-06T00:00:00Z")]
        ),
        decision_as_of="2025-06-06T00:00:00Z",
        signals=[
            build_signal_input("sec1", "2025-06-06T00:00:00Z", remaining_opportunity_ppm=500000)
        ],
        allow_fixture=True,
    )
    assert final.status == "REUSED"
    with database.connect(read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM trade_proposal").fetchone()[0] == 1
    # deterministic priority: verified break beats score band regardless of order
    state = BudgetState("2025-06-01", "fixture-policy-v1", 1, 5_000_000_000, 0, 0)
    verified = {
        "security_id": "secA",
        "max_notional_microusd": 1_000_000_000,
        "action": "SELL",
        "reason_codes": ["thesis_broken"],
        "category": "verified_break",
    }
    score = {
        "security_id": "secB",
        "max_notional_microusd": 1_000_000_000,
        "action": "BUY",
        "reason_codes": ["score_band"],
        "category": "score_band",
    }
    admitted_a, _ = admit_drafts(state, [score, verified], policy, starting_cash_microusd=None)
    admitted_b, _ = admit_drafts(
        BudgetState("2025-06-01", "fixture-policy-v1", 1, 5_000_000_000, 0, 0),
        [verified, score],
        policy,
        starting_cash_microusd=None,
    )
    assert [d["security_id"] for d in admitted_a] == ["secA"]
    assert [d["security_id"] for d in admitted_b] == ["secA"]


# ---------------------------------------------------------------------------
# 24. proposal contract
# ---------------------------------------------------------------------------


def ra03_24_proposal_typed_paper_fields(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
        _seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        _seed_score(conn, pipeline_run_id="run1", security_id="sec1", suffix="a")
    engine = PortfolioEngine(database)
    snapshot = _empty_snapshot(
        "2025-06-01T00:00:00Z", market=[_market_input("sec1", "2025-06-01T00:00:00Z")]
    )
    signal = build_signal_input("sec1", "2025-06-01T00:00:00Z", remaining_opportunity_ppm=500000)
    engine.run(
        pipeline_run_id="run1",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-01T00:00:00Z",
        signals=[signal],
        allow_fixture=True,
    )
    # drive to ENTER: needs persistence
    for index in range(2, 5):
        run_id = f"run{index}"
        as_of = f"2025-06-{index * 2:02d}T00:00:00Z"
        with database.connect() as conn:
            _seed_pipeline_run(conn, run_id, as_of)
            _seed_score(
                conn,
                pipeline_run_id=run_id,
                security_id="sec1",
                cause="EVIDENCE_DRIVEN",
                evhash=f"e{index}",
                prior=80,
                delta=0,
                suffix=f"r{index}",
                run_as_of=as_of,
            )
        engine.run(
            pipeline_run_id=run_id,
            policy_version="fixture-policy-v1",
            snapshot=_empty_snapshot(as_of, market=[_market_input("sec1", as_of)]),
            decision_as_of=as_of,
            signals=[build_signal_input("sec1", as_of, remaining_opportunity_ppm=500000)],
            allow_fixture=True,
        )
    with database.connect(read_only=True) as conn:
        proposal = conn.execute("SELECT * FROM trade_proposal").fetchone()
        assert proposal is not None
        assert proposal["proposal_mode"] == "PAPER"
        assert proposal["requires_human_approval"] == 1
        assert proposal["action"] == "BUY"
        assert proposal["current_state"] == "WATCH"
        assert proposal["proposed_state"] == "ENTER"
        assert proposal["target_weight_ppm"] > proposal["current_weight_ppm"]
        assert proposal["max_quantity_microunits"] > 0
        assert proposal["max_notional_microusd"] > 0
        assert proposal["score_snapshot_id"]
        assert proposal["portfolio_snapshot_id"]
        assert proposal["policy_version"] == "fixture-policy-v1"
        assert proposal["sizing_policy_version"]
        constraints = json.loads(proposal["order_constraints_json"])
        assert constraints == {
            "paper_only": True,
            "long_only": True,
            "limit_only": True,
            "quantity_increment_microunits": 1_000_000,
        }
        # lineage: proposal -> decision -> observation -> run -> snapshot
        decision_id = proposal["decision_id"]
        observation = conn.execute(
            "SELECT portfolio_snapshot_id,policy_version FROM portfolio_state_observation "
            "WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        assert observation is not None
        assert observation["portfolio_snapshot_id"] == proposal["portfolio_snapshot_id"]


# ---------------------------------------------------------------------------
# 25. briefing
# ---------------------------------------------------------------------------


def ra03_25_briefing_deterministic_and_safe(tmp: Path) -> None:
    database = _runtime(tmp)
    with database.connect() as conn:
        _seed_security(conn, "sec1")
        _seed_bars(conn, "sec1", _closes())
        _seed_pipeline_run(conn, "run1", "2025-06-01T00:00:00Z")
        _seed_score(
            conn,
            pipeline_run_id="run1",
            security_id="sec1",
            conviction=30,
            trajectory="STABLE",
            suffix="a",
        )
    engine = PortfolioEngine(database)
    snapshot = _empty_snapshot(
        "2025-06-01T00:00:00Z", market=[_market_input("sec1", "2025-06-01T00:00:00Z")]
    )
    first = engine.run(
        pipeline_run_id="run1",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-01T00:00:00Z",
        allow_fixture=True,
    )
    second = engine.run(
        pipeline_run_id="run1",
        policy_version="fixture-policy-v1",
        snapshot=snapshot,
        decision_as_of="2025-06-01T00:00:00Z",
        allow_fixture=True,
    )
    assert first.briefing == second.briefing
    assert first.briefing_hash == second.briefing_hash
    for section in (
        "DATA STATUS",
        "PORTFOLIO STATUS",
        "CHANGES",
        "PROPOSALS",
        "BLOCKED / NEEDS ATTENTION",
    ):
        assert section in first.briefing
    assert "No portfolio action recommended." in first.briefing  # first-class no-action
    for forbidden in ("confirmation", "token=", "submit_order", "TIGEROPEN", "private_key"):
        assert forbidden not in first.briefing.lower()


# ---------------------------------------------------------------------------
# 26. execution boundary
# ---------------------------------------------------------------------------


def ra03_26_no_execution_leakage(tmp: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    forbidden_imports = {"tradehub"}
    forbidden_identifiers = {
        bytes.fromhex(h).decode()
        for h in (
            "7375626d69745f6f72646572",  # submit_order
            "636f6e6669726d6174696f6e5f746f6b656e",  # confirmation_token
        )
    }
    forbidden_strings = {
        bytes.fromhex(h).decode()
        for h in (
            "2f6f72646572732f70726576696577",  # /orders/preview
            "2f6f72646572732f7375626d6974",  # /orders/submit
        )
    }
    forbidden_env = {
        bytes.fromhex(h).decode()
        for h in (
            "54494745524f50454e5f",  # TIGEROPEN_
            "54524144454855425f4150495f544f4b454e",  # TRADEHUB_API_TOKEN
        )
    }
    sanctioned = {
        "tradehub_research/committee/capability.py",
        "tradehub_research/acceptance/packs/ra00.py",
        "tradehub_research/acceptance/packs/ra02.py",
        "tradehub_research/acceptance/sanitize.py",
        "tests/test_research_capability.py",
        "tests/test_acceptance.py",
        "tests/test_research_spine.py",
        "tests/test_audit.py",
        "tests/test_config.py",
        "tests/test_mcp_server.py",
        "tests/test_mcp_server_import.py",
        "tests/test_order_flow.py",
        "tests/test_policy.py",
        "tests/test_read_only_api.py",
        "tests/test_telegram_bot.py",
        "tests/test_tiger_gateway.py",
        "tests/test_portfolio_boundary.py",
        "tradehub_research/acceptance/packs/ra03.py",
    }
    violations: list[str] = []
    for root in (repo_root / "tradehub_research", repo_root / "tests"):
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(repo_root).as_posix()
            if relative in sanctioned:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in forbidden_imports:
                            violations.append(f"{relative}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in forbidden_imports:
                        violations.append(f"{relative}: from {node.module}")
                elif isinstance(node, ast.Name) and node.id in forbidden_identifiers:
                    violations.append(f"{relative}:{node.lineno}: {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in forbidden_identifiers:
                    violations.append(f"{relative}:{node.lineno}: {node.attr}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    lowered = node.value.lower()
                    if any(fragment in lowered for fragment in forbidden_strings):
                        violations.append(f"{relative}:{node.lineno}: forbidden string")
                    if any(lowered.startswith(prefix.lower()) for prefix in forbidden_env):
                        violations.append(f"{relative}:{node.lineno}: forbidden env name")
    assert not violations, "execution-boundary violations:\n" + "\n".join(violations)

    import tradehub_research.config as config_module

    fields = set(config_module.ResearchSettings.model_fields.keys())
    leaked = {name for name in forbidden_env if name.lower() in fields}
    assert not leaked, f"execution env names in ResearchSettings: {leaked}"


ASSERTIONS: list[tuple[str, object]] = [
    ("RA03-01", ra03_01_migration_and_append_only),
    ("RA03-02", ra03_02_policy_hash_idempotence_and_collision),
    ("RA03-03", ra03_03_policy_fail_closed),
    ("RA03-04", ra03_04_snapshot_contract),
    ("RA03-05", ra03_05_state_derivation_and_edges),
    ("RA03-06", ra03_06_identical_input_idempotent),
    ("RA03-07", ra03_07_persistence_counts_evidence_driven),
    ("RA03-08", ra03_08_unchanged_evidence_and_rebases_do_not_count),
    ("RA03-09", ra03_09_material_change_bypass_directional),
    ("RA03-10", ra03_10_cooldown_boundaries),
    ("RA03-11", ra03_11_thesis_break_verified_only),
    ("RA03-12", ra03_12_score_alone_cannot_trade),
    ("RA03-13", ra03_13_sell_reason_whitelist),
    ("RA03-14", ra03_14_no_sell_without_holdings),
    ("RA03-15", ra03_15_concentration_blocks),
    ("RA03-16", ra03_16_correlation_blocks),
    ("RA03-17", ra03_17_volatility_explicit),
    ("RA03-18", ra03_18_liquidity_clips),
    ("RA03-19", ra03_19_factor_drawdown_seams),
    ("RA03-20", ra03_20_sizing_nonlinear_and_cash),
    ("RA03-21", ra03_21_budget_count_cap_survives_restart),
    ("RA03-22", ra03_22_budget_notional_cap_cannot_be_crossed),
    ("RA03-23", ra03_23_budget_duplicate_consumes_once_and_day_mismatch),
    ("RA03-24", ra03_24_proposal_typed_paper_fields),
    ("RA03-25", ra03_25_briefing_deterministic_and_safe),
    ("RA03-26", ra03_26_no_execution_leakage),
]

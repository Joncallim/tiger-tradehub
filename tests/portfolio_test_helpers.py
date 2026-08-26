"""Shared seeding helpers for portfolio-plane tests (not collected by pytest).

Seeds the minimal committee->score chain the engine needs: security,
evidence, pipeline_run, candidate, committee_run, model_assessment,
comparison_report, and score_snapshot rows with deterministic content.
"""

from __future__ import annotations

from typing import Any

from tradehub_research.portfolio.types import C, D, json_roundtrip
from tradehub_research.screens import canonical_json

SCORE_TAG = "score-snapshot-v1"


def seed_security(
    db: Any,
    security_id: str,
    *,
    ticker: str | None = None,
    sector: str | None = "Tech",
    coverage: str = "SUPPORTED",
    delisted_at: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO security VALUES (?,?,?,?,?,?,?,?,?)",
        (
            security_id,
            ticker or security_id.upper(),
            "NYSE",
            f"{security_id} Inc",
            sector,
            None,
            coverage,
            "2024-01-01",
            delisted_at,
        ),
    )


def seed_evidence(
    db: Any,
    security_id: str,
    *,
    record_type: str,
    session_date: str,
    pat: str,
    evidence_id: str,
    fields: dict[str, Any],
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO evidence_source VALUES (?,?,?,?,?)",
        ("tiingo_eod", "market_data", 1, None, "derived_from_index"),
    )
    structured = {"record_type": record_type, "provider_ticker": security_id.upper(), **fields}
    db.execute(
        "INSERT INTO evidence_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            evidence_id,
            security_id,
            "tiingo_eod",
            canonical_json(structured),
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


def seed_price_bars(
    db: Any,
    security_id: str,
    *,
    closes: list[float],
    volumes: list[int] | None = None,
    start_date: str = "2025-01-02",
    pat_prefix: str = "2025",
) -> list[str]:
    """Seed N daily price bars with deterministic session dates and PATs."""
    from datetime import date, timedelta

    volumes = volumes or [1_000_000] * len(closes)
    ids: list[str] = []
    session = date.fromisoformat(start_date)
    for index, (close, volume) in enumerate(zip(closes, volumes, strict=False)):
        session_text = session.isoformat()
        pat = f"{session_text}T21:00:00Z"
        evidence_id = f"{security_id}:bar:{index:03d}"
        seed_evidence(
            db,
            security_id,
            record_type="price_bar",
            session_date=session_text,
            pat=pat,
            evidence_id=evidence_id,
            fields={
                "session_date": session_text,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
            },
        )
        ids.append(evidence_id)
        session += timedelta(days=1)
    return ids


def seed_pipeline_run(db: Any, run_id: str, as_of: str) -> None:
    db.execute(
        "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,"
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


def seed_score(
    db: Any,
    *,
    pipeline_run_id: str,
    security_id: str,
    conviction: int = 80,
    data_quality: float = 0.9,
    agreement: float = 0.8,
    trajectory_label: str = "RISING",
    change_cause: str = "INITIAL",
    material_change_time: str | None = None,
    prior_conviction: int | None = None,
    conviction_delta: int | None = None,
    scored_evidence_hash: str | None = None,
    run_as_of: str | None = None,
    committee_suffix: str = "a",
) -> str:
    """Seed the committee->score chain for one security; returns snapshot_id."""
    run_as_of = run_as_of or "2025-06-01T00:00:00Z"
    evidence_hash = scored_evidence_hash or C(
        {"security_id": security_id, "evidence": [f"{security_id}:e:{committee_suffix}"]}
    )
    candidate_id = f"cand-{security_id}-{committee_suffix}"
    committee_run_id = f"cr-{security_id}-{committee_suffix}"
    comparison_id = f"cmp-{security_id}-{committee_suffix}"
    scoring_config_hash = f"sc-{security_id}-{committee_suffix}"
    comparator_config_hash = f"cc-{security_id}-{committee_suffix}"
    pack_hash = f"pack-{security_id}-{committee_suffix}"
    assessment_a = f"as-a-{security_id}-{committee_suffix}"
    assessment_b = f"as-b-{security_id}-{committee_suffix}"
    screen_definition_hash = f"sd-{security_id}-{committee_suffix}"
    scoring_version_number = 1 + sum(ord(ch) for ch in committee_suffix)
    comparator_version_number = 1 + sum(ord(ch) for ch in committee_suffix)

    db.execute(
        "INSERT INTO scoring_version(config_hash,scoring_version,spec_json,description,created_at)"
        " VALUES (?,?,?,?,?)",
        (scoring_config_hash, scoring_version_number, '{"v":1}', "fixture", "2025-01-01T00:00:00Z"),
    )
    db.execute(
        "INSERT INTO comparator_definition(config_hash,comparator_version,taxonomy_version,"
        "spec_json,created_at) VALUES (?,?,?,?,?)",
        (comparator_config_hash, comparator_version_number, 1, '{"v":1}', "2025-01-01T00:00:00Z"),
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
            1,
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
                    "evidence": [{"evidence_id": f"{security_id}:e:{committee_suffix}"}],
                    "screens": [
                        {
                            "family": "valuation",
                            "screen_id": "value",
                            "version": 1,
                            "passed": True,
                            "evidence_ids": [f"{security_id}:e:{committee_suffix}"],
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
                json_roundtrip([f"{security_id}:e:{committee_suffix}"]),
                "[]",
                '{"summary":"s","upside_mechanism":"u","downside_mechanism":"d","thesis_break_conditions":[]}',
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
    snapshot_id = D(SCORE_TAG, score_input_hash)
    delta = (
        conviction_delta
        if conviction_delta is not None
        else (conviction - prior_conviction if prior_conviction is not None else None)
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
            prior_conviction,
            delta,
            trajectory_label,
            change_cause,
            material_change_time,
            "[]",
            "result",
            run_as_of,
        ),
    )
    return snapshot_id


def seed_thesis_break(
    db: Any,
    *,
    security_id: str,
    score_snapshot_id: str,
    status: str = "VERIFIED",
    method: str = "FIXTURE",
    verified_at: str = "2025-06-02T00:00:00Z",
    condition_id: str = "cond-1",
    verifier_ref: str = "fixture-verifier",
) -> tuple[str, str]:
    """Seed a thesis-break event + verification; returns (event_id, verification_id)."""
    event_material = {
        "security_id": security_id,
        "condition_id": condition_id,
        "condition_text": "fixture condition text",
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
            condition_id,
            event_material["condition_text"],
            json_roundtrip(event_material["evidence_ids"]),
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
        "verifier_ref": verifier_ref,
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
            verifier_ref,
            C(verification_material),
            verified_at,
        ),
    )
    return event_id, verification_id

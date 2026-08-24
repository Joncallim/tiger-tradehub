"""Persistence and deterministic recovery for the Phase 1 screening ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from tradehub_research.db import ResearchDB, normalize_ts, utc_now
from tradehub_research.screens import ScreenResult, ScreenSpec, canonical_json


class DeterminismError(RuntimeError):
    """Stored bytes disagree with a row having the same logical identity."""


class RunStateError(RuntimeError):
    """An operation is incompatible with the pipeline run's state."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ScreenStore:
    def __init__(self, database: ResearchDB):
        self.database = database

    def save_screen_definition(self, spec: ScreenSpec) -> str:
        spec_json = spec.canonical_json()
        config_hash = spec.config_hash
        with self.database.connect() as db:
            by_hash = db.execute(
                "SELECT family,screen_id,screen_version,spec_json FROM screen_definition "
                "WHERE config_hash=?",
                (config_hash,),
            ).fetchone()
            by_version = db.execute(
                "SELECT config_hash,spec_json FROM screen_definition "
                "WHERE family=? AND screen_id=? AND screen_version=?",
                (spec.family, spec.screen_id, spec.screen_version),
            ).fetchone()
            if by_hash is not None:
                expected = (spec.family, spec.screen_id, spec.screen_version, spec_json)
                if tuple(by_hash) != expected:
                    raise DeterminismError("config_hash reused with different ScreenSpec bytes")
                return config_hash
            if by_version is not None:
                raise DeterminismError("screen family/id/version reused with different bytes")
            db.execute(
                "INSERT INTO screen_definition(config_hash,family,screen_id,screen_version,"
                "spec_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    config_hash,
                    spec.family,
                    spec.screen_id,
                    spec.screen_version,
                    spec_json,
                    utc_now(),
                ),
            )
        return config_hash

    def begin_run(
        self,
        *,
        as_of: str,
        universe_hash: str,
        screen_manifest: Sequence[dict[str, Any]],
        funnel_config: dict[str, Any],
        input_view_hash: str,
        expected_security_count: int,
        input_snapshot_id: str | None = None,
    ) -> str:
        if expected_security_count < 0:
            raise ValueError("expected_security_count must not be negative")
        normalized_as_of = normalize_ts(as_of)
        manifest_json = canonical_json(list(screen_manifest))
        manifest_hash = _hash(manifest_json)
        funnel_json = canonical_json(funnel_config)
        funnel_hash = _hash(funnel_json)
        logical_material = "".join(
            (normalized_as_of, universe_hash, manifest_hash, funnel_hash, input_view_hash)
        )
        run_id = _hash(f"pipeline-v1\0{logical_material}")
        immutable = (
            normalized_as_of,
            universe_hash,
            manifest_json,
            manifest_hash,
            funnel_json,
            funnel_hash,
            input_snapshot_id,
            input_view_hash,
            expected_security_count,
        )
        with self.database.connect() as db:
            row = db.execute(
                "SELECT as_of,universe_hash,screen_manifest_json,screen_manifest_hash,"
                "funnel_config_json,funnel_config_hash,input_snapshot_id,input_view_hash,"
                "expected_security_count,status FROM pipeline_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is not None:
                if tuple(row[:9]) != immutable:
                    raise DeterminismError("run_id reused with different logical inputs")
                if row[9] == "FAILED":
                    raise RunStateError("failed pipeline run is terminal")
                return run_id
            db.execute(
                "INSERT INTO pipeline_run(run_id,as_of,universe_hash,screen_manifest_json,"
                "screen_manifest_hash,funnel_config_json,funnel_config_hash,input_snapshot_id,"
                "input_view_hash,expected_security_count,status,failure_json,started_at,finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'RUNNING',NULL,?,NULL)",
                (run_id, *immutable, utc_now()),
            )
        return run_id

    def persist_screen_population(
        self, run_id: str, config_hash: str, results: Iterable[ScreenResult]
    ) -> None:
        population = list(results)
        try:
            with self.database.connect() as db:
                run = db.execute(
                    "SELECT status FROM pipeline_run WHERE run_id=?", (run_id,)
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown pipeline run: {run_id}")
                if run[0] == "FAILED":
                    raise RunStateError("failed pipeline run is terminal")
                for result in population:
                    result.verify()
                    if result.run_id != run_id or result.config_hash != config_hash:
                        raise DeterminismError("result does not belong to this run and screen")
                    values = self._result_values(result)
                    stored = db.execute(
                        "SELECT screen_result_id,run_id,security_id,config_hash,raw_features_json,"
                        "evidence_ids_json,reason_codes_json,sufficient_data,passed,confidence,"
                        "data_quality,result_hash FROM screen_result "
                        "WHERE run_id=? AND security_id=? AND config_hash=?",
                        (run_id, result.security_id, config_hash),
                    ).fetchone()
                    if stored is not None:
                        if tuple(stored) != values[:-1]:
                            raise DeterminismError(
                                "stored screen result differs from deterministic retry"
                            )
                        continue
                    db.execute(
                        "INSERT INTO screen_result(screen_result_id,run_id,security_id,config_hash,"
                        "raw_features_json,evidence_ids_json,reason_codes_json,sufficient_data,"
                        "passed,confidence,data_quality,result_hash,computed_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
        except DeterminismError as exc:
            self.fail_run(run_id, str(exc))
            raise
        except sqlite3.IntegrityError as exc:
            # A competing writer may have filled the identity with different bytes.
            error = DeterminismError(f"screen population integrity conflict: {exc}")
            self.fail_run(run_id, str(error))
            raise error from exc

    @staticmethod
    def _result_values(result: ScreenResult) -> tuple[object, ...]:
        return (
            result.screen_result_id,
            result.run_id,
            result.security_id,
            result.config_hash,
            canonical_json(result.raw_features),
            canonical_json(result.evidence_ids),
            canonical_json(result.reason_codes),
            int(result.sufficient_data),
            int(result.passed),
            result.confidence,
            result.data_quality,
            result.result_hash,
            normalize_ts(result.computed_at),
        )

    def count_screen_results(self, run_id: str, config_hash: str) -> int:
        with self.database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM screen_result WHERE run_id=? AND config_hash=?",
                (run_id, config_hash),
            ).fetchone()
        return int(row[0])

    def verify_screen_population(
        self, run_id: str, config_hash: str, expected_security_ids: Iterable[str]
    ) -> bool:
        expected = set(expected_security_ids)
        with self.database.connect(read_only=True) as db:
            actual = {
                row[0]
                for row in db.execute(
                    "SELECT security_id FROM screen_result WHERE run_id=? AND config_hash=?",
                    (run_id, config_hash),
                )
            }
        return actual == expected

    def verify_expected_counts(self, run_id: str) -> bool:
        with self.database.connect(read_only=True) as db:
            run = db.execute(
                "SELECT screen_manifest_json,expected_security_count "
                "FROM pipeline_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown pipeline run: {run_id}")
            manifest = json.loads(run[0])
            default_count = int(run[1])
            for entry in manifest:
                expected = int(entry.get("expected_count", default_count))
                count = db.execute(
                    "SELECT COUNT(*) FROM screen_result WHERE run_id=? AND config_hash=?",
                    (run_id, entry["config_hash"]),
                ).fetchone()[0]
                if count != expected:
                    return False
        return True

    def complete_run(self, run_id: str) -> None:
        if not self.verify_expected_counts(run_id):
            raise RunStateError("screen populations are incomplete")
        with self.database.connect() as db:
            row = db.execute("SELECT status FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown pipeline run: {run_id}")
            if row[0] == "COMPLETE":
                return
            if row[0] == "FAILED":
                raise RunStateError("failed pipeline run is terminal")
            db.execute(
                "UPDATE pipeline_run SET status='COMPLETE',finished_at=? WHERE run_id=?",
                (utc_now(), run_id),
            )

    def fail_run(self, run_id: str, reason: str | dict[str, Any]) -> None:
        failure = reason if isinstance(reason, dict) else {"reason": reason}
        with self.database.connect() as db:
            row = db.execute("SELECT status FROM pipeline_run WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown pipeline run: {run_id}")
            if row[0] == "FAILED":
                return
            if row[0] == "COMPLETE":
                raise RunStateError("completed pipeline run cannot fail")
            db.execute(
                "UPDATE pipeline_run SET status='FAILED',failure_json=?,finished_at=? "
                "WHERE run_id=?",
                (canonical_json(failure), utc_now(), run_id),
            )

    # ------------------------------------------------------------------
    # I2: funnel-facing reads and candidate persistence
    # ------------------------------------------------------------------

    def logical_material(self, run_id: str) -> str:
        """as_of || universe_hash || screen_manifest_hash || funnel_config_hash
        || input_view_hash — the exact material hashed into run_id (design 5.4)."""
        with self.database.connect(read_only=True) as db:
            row = db.execute(
                "SELECT as_of,universe_hash,screen_manifest_hash,funnel_config_hash,"
                "input_view_hash FROM pipeline_run WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown pipeline run: {run_id}")
        return "".join(row)

    def load_results_for_funnel(self, run_id: str) -> list[dict[str, Any]]:
        """All results for the run joined to their screen family (bounded: one
        query, not per-security)."""
        with self.database.connect(read_only=True) as db:
            rows = db.execute(
                "SELECT r.security_id,d.family,r.screen_result_id,r.sufficient_data,"
                "r.passed,r.confidence,r.data_quality,r.evidence_ids_json "
                "FROM screen_result r JOIN screen_definition d "
                "ON d.config_hash=r.config_hash WHERE r.run_id=? "
                "ORDER BY r.security_id,d.family",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cluster_ids_by_evidence(self, as_of: str | None = None) -> dict[str, set[str]]:
        """One bounded scan resolving every evidence cluster membership.

        Maps evidence_id -> set(cluster_id).  Cluster ids are resolved at rank
        time only and are never copied into screen_result rows (design 3).
        """
        with self.database.connect(read_only=True) as db:
            if as_of is None:
                rows = db.execute(
                    "SELECT evidence_id,cluster_id FROM evidence_cluster_member "
                    "ORDER BY evidence_id,cluster_id"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT m.evidence_id,m.cluster_id FROM evidence_cluster_member m "
                    "JOIN evidence_event e ON e.evidence_id=m.evidence_id "
                    "WHERE e.public_available_time IS NOT NULL AND e.public_available_time <= ? "
                    "AND e.pat_provenance IN ('source_reported','derived_from_index') "
                    "ORDER BY m.evidence_id,m.cluster_id",
                    (normalize_ts(as_of),),
                ).fetchall()
        lookup: dict[str, set[str]] = {}
        for row in rows:
            lookup.setdefault(row["evidence_id"], set()).add(row["cluster_id"])
        return lookup

    def persist_run_flags(self, run_id: str, flags: Iterable[str]) -> None:
        """Persist deterministic funnel conditions on a completed run."""
        value = canonical_json(sorted(set(flags)))
        with self.database.connect() as db:
            row = db.execute(
                "SELECT status,flags_json FROM pipeline_run WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown pipeline run: {run_id}")
            if row[0] != "COMPLETE":
                raise RunStateError("run flags require a COMPLETE pipeline run")
            if row[1] is not None and row[1] != value:
                raise DeterminismError("stored run flags differ from deterministic retry")
            if row[1] is None:
                db.execute("UPDATE pipeline_run SET flags_json=? WHERE run_id=?", (value, run_id))

    def persist_candidates(self, run_id: str, candidates: Iterable[Any]) -> None:
        """Insert-or-verify candidate rows in one transaction (design 6: the
        funnel runs in a separate transaction after COMPLETE; retry verifies)."""
        rows = list(candidates)
        try:
            with self.database.connect() as db:
                run = db.execute(
                    "SELECT status FROM pipeline_run WHERE run_id=?", (run_id,)
                ).fetchone()
                if run is None:
                    raise KeyError(f"unknown pipeline run: {run_id}")
                if run[0] != "COMPLETE":
                    raise RunStateError("funnel requires a COMPLETE pipeline run")
                for candidate in rows:
                    values = (
                        candidate.candidate_id,
                        run_id,
                        candidate.security_id,
                        candidate.ordinal,
                        canonical_json(candidate.inclusion_reasons),
                        canonical_json(candidate.screen_result_ids),
                        canonical_json(candidate.rank_telemetry),
                        int(candidate.is_control),
                        candidate.control_algorithm,
                        candidate.control_key,
                        candidate.control_rank,
                    )
                    stored = db.execute(
                        "SELECT candidate_id,run_id,security_id,ordinal,inclusion_reasons_json,"
                        "screen_result_ids_json,rank_telemetry_json,is_control,"
                        "control_algorithm,control_key,control_rank FROM candidate "
                        "WHERE run_id=? AND security_id=?",
                        (run_id, candidate.security_id),
                    ).fetchone()
                    if stored is not None:
                        if tuple(stored) != values:
                            raise DeterminismError(
                                "stored candidate differs from deterministic retry"
                            )
                        continue
                    db.execute(
                        "INSERT INTO candidate(candidate_id,run_id,security_id,ordinal,"
                        "inclusion_reasons_json,screen_result_ids_json,rank_telemetry_json,"
                        "is_control,control_algorithm,control_key,control_rank,included_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*values, utc_now()),
                    )
        except DeterminismError:
            # Candidate rows are intentionally written after COMPLETE.  A retry
            # conflict must retain its useful error instead of trying to fail an
            # already terminal screen run and masking it with RunStateError.
            raise
        except sqlite3.IntegrityError as exc:
            error = DeterminismError(f"candidate integrity conflict: {exc}")
            raise error from exc

    def load_candidates(self, run_id: str) -> list[dict[str, Any]]:
        with self.database.connect(read_only=True) as db:
            rows = db.execute(
                "SELECT * FROM candidate WHERE run_id=? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

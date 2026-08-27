"""experiment.db schema -- append-only validation-engine storage boundary.

This is a WHOLLY SEPARATE database from research.db. Historical/backtest
evaluation reads research.db read-only (or a frozen snapshot of it) and
never mutates it; every experiment write goes here. Follows the exact
(version:int, description:str, sql:str) migration shape as
tradehub_research/schema.py, with append-only triggers and canonical-hash
primary keys matching that file's conventions.
"""

from __future__ import annotations

# ruff: noqa: E501 -- migration SQL remains legible as exact DDL statements.

VALIDATION_SCHEMA_VERSION = 1

VALIDATION_MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "Phase 5 validation engine: dataset snapshots, regimes, attempts, metrics",
        """
        CREATE TABLE universe_sample (
            sample_id TEXT PRIMARY KEY,
            source_pool_ref TEXT NOT NULL,
            source_pool_content_hash TEXT NOT NULL,
            seed INTEGER NOT NULL,
            algorithm TEXT NOT NULL,
            requested_size INTEGER NOT NULL CHECK(requested_size > 0),
            selected_tickers_json TEXT NOT NULL CHECK(json_valid(selected_tickers_json)),
            selected_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER universe_sample_no_update BEFORE UPDATE ON universe_sample
        BEGIN SELECT RAISE(ABORT,'universe_sample is append-only'); END;
        CREATE TRIGGER universe_sample_no_delete BEFORE DELETE ON universe_sample
        BEGIN SELECT RAISE(ABORT,'universe_sample is append-only'); END;

        CREATE TABLE dataset_snapshot (
            snapshot_id TEXT PRIMARY KEY,
            source_commit TEXT NOT NULL,
            source_db_schema_version INTEGER NOT NULL,
            universe_sample_id TEXT REFERENCES universe_sample(sample_id),
            manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
            manifest_hash TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            artifact_content_hash TEXT NOT NULL,
            coverage_summary_json TEXT NOT NULL CHECK(json_valid(coverage_summary_json)),
            status TEXT NOT NULL CHECK(status IN ('PENDING','READY')),
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER dataset_snapshot_no_update BEFORE UPDATE ON dataset_snapshot
        WHEN OLD.status='READY'
        BEGIN SELECT RAISE(ABORT,'dataset_snapshot is append-only once READY'); END;
        CREATE TRIGGER dataset_snapshot_no_delete BEFORE DELETE ON dataset_snapshot
        BEGIN SELECT RAISE(ABORT,'dataset_snapshot is append-only'); END;

        CREATE TABLE evaluation_regime (
            regime_id TEXT PRIMARY KEY,
            dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshot(snapshot_id),
            spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
            spec_hash TEXT NOT NULL,
            sealed_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TRIGGER evaluation_regime_seal_only BEFORE UPDATE ON evaluation_regime
        WHEN OLD.regime_id IS NOT NEW.regime_id
          OR OLD.dataset_snapshot_id IS NOT NEW.dataset_snapshot_id
          OR OLD.spec_json IS NOT NEW.spec_json
          OR OLD.spec_hash IS NOT NEW.spec_hash
          OR OLD.created_at IS NOT NEW.created_at
          OR OLD.sealed_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'evaluation_regime only permits one seal transition, dates never change'); END;
        CREATE TRIGGER evaluation_regime_no_delete BEFORE DELETE ON evaluation_regime
        BEGIN SELECT RAISE(ABORT,'evaluation_regime is append-only'); END;

        CREATE TABLE experiment_attempt (
            attempt_id TEXT PRIMARY KEY,
            regime_id TEXT NOT NULL REFERENCES evaluation_regime(regime_id),
            dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshot(snapshot_id),
            variant_kind TEXT NOT NULL CHECK(variant_kind IN
                ('BASELINE','HUNTER_EVAL','ABLATION','WALKFORWARD_FOLD','HOLDOUT')),
            variant_name TEXT NOT NULL,
            config_json TEXT NOT NULL CHECK(json_valid(config_json)),
            config_hash TEXT NOT NULL,
            fold_id TEXT,
            horizon_sessions INTEGER CHECK(horizon_sessions IS NULL OR horizon_sessions IN (21,63,126,252)),
            attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
            status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETE','FAILED','INSUFFICIENT_DATA')),
            failure_json TEXT CHECK(failure_json IS NULL OR json_valid(failure_json)),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(regime_id, variant_name, config_hash, fold_id, attempt_number)
        );
        CREATE TRIGGER experiment_attempt_immutable_inputs BEFORE UPDATE ON experiment_attempt
        WHEN OLD.attempt_id IS NOT NEW.attempt_id
          OR OLD.regime_id IS NOT NEW.regime_id
          OR OLD.variant_kind IS NOT NEW.variant_kind
          OR OLD.config_hash IS NOT NEW.config_hash
        BEGIN SELECT RAISE(ABORT,'experiment_attempt logical inputs are immutable'); END;
        CREATE TRIGGER experiment_attempt_no_delete BEFORE DELETE ON experiment_attempt
        BEGIN SELECT RAISE(ABORT,'experiment_attempt is append-only'); END;
        CREATE TRIGGER experiment_attempt_seal_guard BEFORE INSERT ON experiment_attempt
        WHEN NEW.variant_kind != 'HOLDOUT' AND (
            SELECT sealed_at FROM evaluation_regime WHERE regime_id = NEW.regime_id) IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'regime sealed: only the pre-registered HOLDOUT attempt may run'); END;

        CREATE TABLE metric (
            metric_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES experiment_attempt(attempt_id),
            horizon_sessions INTEGER NOT NULL,
            segment TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            point_estimate REAL NOT NULL,
            ci_lower REAL,
            ci_upper REAL,
            bootstrap_seed INTEGER,
            bootstrap_method TEXT CHECK(bootstrap_method IS NULL OR bootstrap_method IN ('stationary','moving_block')),
            date_count INTEGER,
            security_count INTEGER,
            effective_n REAL,
            low_confidence INTEGER NOT NULL CHECK(low_confidence IN (0,1)),
            computed_at TEXT NOT NULL,
            UNIQUE(attempt_id, horizon_sessions, segment, metric_name)
        );
        CREATE TRIGGER metric_no_update BEFORE UPDATE ON metric
        BEGIN SELECT RAISE(ABORT,'metric is append-only'); END;
        CREATE TRIGGER metric_no_delete BEFORE DELETE ON metric
        BEGIN SELECT RAISE(ABORT,'metric is append-only'); END;

        CREATE TABLE benchmark_artifact (
            benchmark_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            vintage_label TEXT NOT NULL,
            raw_content_hash TEXT NOT NULL,
            parsed_series_hash TEXT NOT NULL,
            cache_path TEXT NOT NULL,
            retrieved_at TEXT NOT NULL
        );
        CREATE TRIGGER benchmark_artifact_no_update BEFORE UPDATE ON benchmark_artifact
        BEGIN SELECT RAISE(ABORT,'benchmark_artifact is append-only'); END;
        CREATE TRIGGER benchmark_artifact_no_delete BEFORE DELETE ON benchmark_artifact
        BEGIN SELECT RAISE(ABORT,'benchmark_artifact is append-only'); END;

        CREATE TABLE outcome_label (
            label_id TEXT PRIMARY KEY,
            dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshot(snapshot_id),
            security_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            horizon_sessions INTEGER NOT NULL CHECK(horizon_sessions IN (21,63,126,252)),
            entry_convention TEXT NOT NULL CHECK(entry_convention IN
                ('next_session_open','next_session_close_fallback')),
            entry_session_date TEXT,
            entry_price_evidence_ref TEXT,
            exit_session_date TEXT,
            exit_price_evidence_ref TEXT,
            raw_return REAL,
            total_return REAL,
            benchmark_id TEXT REFERENCES benchmark_artifact(benchmark_id),
            benchmark_return REAL,
            benchmark_relative_return REAL,
            outcome_status TEXT NOT NULL CHECK(outcome_status IN
                ('OBSERVED','DELISTING_OUTCOME_UNKNOWN','CENSORED_INSUFFICIENT_HORIZON','ENTRY_UNAVAILABLE')),
            delisting_event_ref TEXT,
            builder_version TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            UNIQUE(security_id, observation_date, horizon_sessions, dataset_snapshot_id, builder_version)
        );
        CREATE TRIGGER outcome_label_no_update BEFORE UPDATE ON outcome_label
        BEGIN SELECT RAISE(ABORT,'outcome_label is append-only'); END;
        CREATE TRIGGER outcome_label_no_delete BEFORE DELETE ON outcome_label
        BEGIN SELECT RAISE(ABORT,'outcome_label is append-only'); END;

        CREATE TABLE lookahead_canary_run (
            run_id TEXT PRIMARY KEY,
            dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshot(snapshot_id),
            canary_kind TEXT NOT NULL,
            detected INTEGER NOT NULL CHECK(detected IN (0,1)),
            detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
            run_at TEXT NOT NULL
        );
        CREATE TRIGGER lookahead_canary_run_no_update BEFORE UPDATE ON lookahead_canary_run
        BEGIN SELECT RAISE(ABORT,'lookahead_canary_run is append-only'); END;
        CREATE TRIGGER lookahead_canary_run_no_delete BEFORE DELETE ON lookahead_canary_run
        BEGIN SELECT RAISE(ABORT,'lookahead_canary_run is append-only'); END;

        CREATE TABLE backfill_attempt (
            attempt_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL CHECK(provider IN ('tiingo','sec')),
            symbol_or_cik TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('SUCCESS','ERROR','SKIPPED_QUOTA')),
            http_status INTEGER,
            bytes INTEGER,
            error TEXT,
            requested_at TEXT NOT NULL
        );
        CREATE TRIGGER backfill_attempt_no_update BEFORE UPDATE ON backfill_attempt
        BEGIN SELECT RAISE(ABORT,'backfill_attempt is append-only'); END;
        CREATE TRIGGER backfill_attempt_no_delete BEFORE DELETE ON backfill_attempt
        BEGIN SELECT RAISE(ABORT,'backfill_attempt is append-only'); END;
        """,
    ),
)

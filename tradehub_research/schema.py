from __future__ import annotations

PHASE_0_SCHEMA_VERSION = 1

MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "V2 Phase 0 evaluation spine and point-in-time evidence foundation",
        """
        CREATE TABLE security (
            security_id TEXT PRIMARY KEY,
            canonical_ticker TEXT NOT NULL,
            exchange TEXT NOT NULL,
            name TEXT NOT NULL,
            sector TEXT,
            industry TEXT,
            sector_coverage_status TEXT NOT NULL CHECK (
                sector_coverage_status IN ('SUPPORTED','LIMITED','RESEARCH_ONLY')
            ),
            first_seen TEXT NOT NULL,
            delisted_at TEXT
        );
        CREATE TABLE security_identity_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id TEXT NOT NULL REFERENCES security(security_id),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'ticker_change','share_class_change','corporate_action','delisting'
            )),
            old_value TEXT, new_value TEXT, event_time TEXT NOT NULL,
            public_available_time TEXT,
            pat_provenance TEXT NOT NULL CHECK (pat_provenance IN (
                'source_reported','derived_from_index','observed_at_ingest','unknown'
            )),
            CHECK (public_available_time IS NOT NULL OR pat_provenance IN (
                'unknown','observed_at_ingest'
            ))
        );
        CREATE TABLE universe_membership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id TEXT NOT NULL REFERENCES security(security_id),
            price REAL, market_cap REAL, avg_dollar_volume REAL,
            price_eligible INTEGER NOT NULL CHECK (price_eligible IN (0,1)),
            market_cap_eligible INTEGER NOT NULL CHECK (market_cap_eligible IN (0,1)),
            liquidity_eligible INTEGER NOT NULL CHECK (liquidity_eligible IN (0,1)),
            eligible INTEGER NOT NULL CHECK (eligible IN (0,1)),
            valid_from TEXT NOT NULL, valid_to TEXT,
            CHECK (valid_to IS NULL OR valid_to > valid_from)
        );
        CREATE TABLE evidence_source (
            source_id TEXT PRIMARY KEY, source_type TEXT NOT NULL,
            hierarchy_tier INTEGER NOT NULL CHECK (hierarchy_tier BETWEEN 1 AND 6),
            reliability_notes TEXT,
            pat_default_provenance TEXT NOT NULL CHECK (pat_default_provenance IN (
                'source_reported','derived_from_index','observed_at_ingest','unknown'
            ))
        );
        CREATE TABLE evidence_event (
            evidence_id TEXT PRIMARY KEY,
            security_id TEXT NOT NULL REFERENCES security(security_id),
            source_id TEXT NOT NULL REFERENCES evidence_source(source_id),
            structured_fields TEXT NOT NULL CHECK (json_valid(structured_fields)),
            extraction_confidence REAL NOT NULL CHECK (
                extraction_confidence >= 0 AND extraction_confidence <= 1
            ),
            supersedes_evidence_id TEXT REFERENCES evidence_event(evidence_id),
            withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0,1)),
            content_hash TEXT NOT NULL,
            event_time TEXT NOT NULL, public_available_time TEXT,
            pat_provenance TEXT NOT NULL CHECK (pat_provenance IN (
                'source_reported','derived_from_index','observed_at_ingest','unknown'
            )),
            ingested_time TEXT NOT NULL,
            UNIQUE (source_id, security_id, content_hash),
            CHECK (public_available_time IS NOT NULL OR pat_provenance IN (
                'unknown','observed_at_ingest'
            )),
            CHECK (withdrawn = 0 OR structured_fields = '{}')
        );
        CREATE TABLE evidence_cluster (
            cluster_id TEXT PRIMARY KEY, representative_summary TEXT NOT NULL,
            formed_at TEXT NOT NULL
        );
        CREATE TABLE evidence_cluster_member (
            evidence_id TEXT NOT NULL REFERENCES evidence_event(evidence_id),
            cluster_id TEXT NOT NULL REFERENCES evidence_cluster(cluster_id),
            PRIMARY KEY (evidence_id, cluster_id)
        );
        CREATE TABLE snapshot_version (
            snapshot_id TEXT PRIMARY KEY, created_from_db_version INTEGER NOT NULL,
            scope_description TEXT NOT NULL, created_at TEXT NOT NULL, content_hash TEXT NOT NULL
        );
        CREATE TABLE experiment_run (
            experiment_id TEXT PRIMARY KEY, name TEXT NOT NULL, config_json TEXT NOT NULL CHECK (
                json_valid(config_json)
            ), scoring_version TEXT,
            input_snapshot_id TEXT REFERENCES snapshot_version(snapshot_id),
            input_hash TEXT NOT NULL, evaluation_window_start TEXT, evaluation_window_end TEXT,
            status TEXT NOT NULL, result_reference TEXT, started_at TEXT NOT NULL, finished_at TEXT
        );
        CREATE TABLE oos_evaluation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL REFERENCES experiment_run(experiment_id),
            attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
            status TEXT NOT NULL, result_reference TEXT, recorded_at TEXT NOT NULL,
            UNIQUE (experiment_id, attempt_number)
        );
        CREATE TABLE sealed_holdout (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT REFERENCES experiment_run(experiment_id),
            description TEXT NOT NULL, sealed_at TEXT NOT NULL
        );
        CREATE INDEX evidence_pit_idx ON evidence_event(
            security_id, public_available_time, pat_provenance
        );
        CREATE INDEX universe_pit_idx ON universe_membership(security_id, valid_from, valid_to);
        CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence_event BEGIN
            SELECT RAISE(ABORT, 'evidence_event is append-only'); END;
        CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence_event BEGIN
            SELECT RAISE(ABORT, 'evidence_event is append-only'); END;
        CREATE TRIGGER experiment_no_update BEFORE UPDATE ON experiment_run BEGIN
            SELECT RAISE(ABORT, 'experiment_run is append-only'); END;
        CREATE TRIGGER experiment_no_delete BEFORE DELETE ON experiment_run BEGIN
            SELECT RAISE(ABORT, 'experiment_run is append-only'); END;
        """,
    ),
)

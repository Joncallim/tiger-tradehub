from __future__ import annotations

PHASE_0_SCHEMA_VERSION = 5

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
    (
        2,
        "Phase 0 independent-review PIT and recovery invariants",
        """
        DROP TRIGGER evidence_no_update;
        DROP TRIGGER evidence_no_delete;
        ALTER TABLE evidence_event RENAME TO evidence_event_v1;
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
            content_hash TEXT NOT NULL, source_record_id TEXT,
            event_time TEXT NOT NULL, public_available_time TEXT,
            pat_provenance TEXT NOT NULL CHECK (pat_provenance IN (
                'source_reported','derived_from_index','observed_at_ingest','unknown'
            )),
            ingested_time TEXT NOT NULL,
            CHECK (public_available_time IS NOT NULL OR pat_provenance IN (
                'unknown','observed_at_ingest'
            )),
            CHECK (withdrawn = 0 OR structured_fields = '{}')
        );
        INSERT INTO evidence_event(
            evidence_id,security_id,source_id,structured_fields,extraction_confidence,
            supersedes_evidence_id,withdrawn,content_hash,event_time,
            public_available_time,pat_provenance,ingested_time
        ) SELECT evidence_id,security_id,source_id,structured_fields,extraction_confidence,
            supersedes_evidence_id,withdrawn,content_hash,event_time,
            public_available_time,pat_provenance,ingested_time FROM evidence_event_v1;
        DROP TABLE evidence_event_v1;
        ALTER TABLE evidence_cluster_member RENAME TO evidence_cluster_member_v1;
        CREATE TABLE evidence_cluster_member (
            evidence_id TEXT NOT NULL REFERENCES evidence_event(evidence_id),
            cluster_id TEXT NOT NULL REFERENCES evidence_cluster(cluster_id),
            PRIMARY KEY (evidence_id, cluster_id)
        );
        INSERT INTO evidence_cluster_member SELECT * FROM evidence_cluster_member_v1;
        DROP TABLE evidence_cluster_member_v1;
        CREATE INDEX evidence_pit_idx ON evidence_event(
            security_id, public_available_time, pat_provenance
        );
        CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence_event BEGIN
            SELECT RAISE(ABORT, 'evidence_event is append-only'); END;
        CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence_event BEGIN
            SELECT RAISE(ABORT, 'evidence_event is append-only'); END;
        ALTER TABLE universe_membership ADD COLUMN knowledge_time TEXT;
        ALTER TABLE universe_membership ADD COLUMN pat_provenance TEXT CHECK (
            pat_provenance IN (
                'source_reported','derived_from_index','observed_at_ingest','unknown'
            )
        );
        ALTER TABLE universe_membership ADD COLUMN supersedes_id INTEGER
            REFERENCES universe_membership(id);

        UPDATE universe_membership SET knowledge_time=valid_from,
            pat_provenance='derived_from_index';

        CREATE UNIQUE INDEX evidence_source_record_uq
            ON evidence_event(source_id, security_id, source_record_id)
            WHERE source_record_id IS NOT NULL;
        CREATE UNIQUE INDEX evidence_fallback_identity_uq
            ON evidence_event(source_id, security_id, event_time, content_hash)
            WHERE source_record_id IS NULL;
        CREATE UNIQUE INDEX evidence_single_successor_uq
            ON evidence_event(supersedes_evidence_id)
            WHERE supersedes_evidence_id IS NOT NULL;
        CREATE INDEX evidence_supersedes_idx
            ON evidence_event(supersedes_evidence_id, public_available_time);
        CREATE INDEX evidence_cluster_member_idx
            ON evidence_cluster_member(cluster_id, evidence_id);
        CREATE INDEX security_identity_event_idx ON security_identity_event(security_id);
        CREATE INDEX evidence_pat_idx ON evidence_event(public_available_time, pat_provenance);

        CREATE TRIGGER evidence_supersession_valid BEFORE INSERT ON evidence_event
        WHEN NEW.supersedes_evidence_id IS NOT NULL BEGIN
            SELECT CASE WHEN NEW.supersedes_evidence_id = NEW.evidence_id
                THEN RAISE(ABORT, 'evidence cannot supersede itself') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM evidence_event p WHERE p.evidence_id=NEW.supersedes_evidence_id
                    AND p.security_id=NEW.security_id AND p.source_id=NEW.source_id
            ) THEN RAISE(ABORT, 'supersession requires same security and source') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM evidence_event p WHERE p.evidence_id=NEW.supersedes_evidence_id
                    AND p.public_available_time IS NOT NULL
                    AND (NEW.public_available_time IS NULL
                         OR NEW.public_available_time < p.public_available_time)
            ) THEN RAISE(ABORT, 'supersession cannot backdate public availability') END;
        END;
        CREATE TRIGGER evidence_pat_before_ingest BEFORE INSERT ON evidence_event
        WHEN NEW.public_available_time IS NOT NULL
             AND NEW.public_available_time > NEW.ingested_time BEGIN
            SELECT RAISE(ABORT, 'public availability cannot follow ingestion');
        END;
        CREATE TRIGGER universe_no_update BEFORE UPDATE ON universe_membership BEGIN
            SELECT RAISE(ABORT, 'universe_membership is append-only'); END;
        CREATE TRIGGER universe_no_delete BEFORE DELETE ON universe_membership BEGIN
            SELECT RAISE(ABORT, 'universe_membership is append-only'); END;
        CREATE TRIGGER universe_required_pit_fields BEFORE INSERT ON universe_membership
        WHEN NEW.knowledge_time IS NULL OR NEW.pat_provenance IS NULL BEGIN
            SELECT RAISE(ABORT, 'membership knowledge time and provenance are required');
        END;
        CREATE TRIGGER universe_supersession_valid BEFORE INSERT ON universe_membership
        WHEN NEW.supersedes_id IS NOT NULL BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM universe_membership p WHERE p.id=NEW.supersedes_id
                    AND p.security_id=NEW.security_id
            ) THEN RAISE(ABORT, 'membership supersession requires same security') END;
        END;

        CREATE TABLE snapshot_manifest (
            snapshot_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            source_db TEXT NOT NULL, content_hash TEXT NOT NULL
        );
        """,
    ),
    (
        3,
        "Enforce universe correction ordering and a single successor",
        """
        CREATE UNIQUE INDEX universe_single_successor_uq
            ON universe_membership(supersedes_id)
            WHERE supersedes_id IS NOT NULL;
        CREATE TRIGGER universe_supersession_knowledge_order BEFORE INSERT
            ON universe_membership
        WHEN NEW.supersedes_id IS NOT NULL BEGIN
            SELECT CASE WHEN NEW.supersedes_id = NEW.id
                THEN RAISE(ABORT, 'membership cannot supersede itself') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM universe_membership p WHERE p.id=NEW.supersedes_id
                    AND p.knowledge_time IS NOT NULL
                    AND NEW.knowledge_time < p.knowledge_time
            ) THEN RAISE(ABORT, 'membership supersession cannot backdate knowledge time') END;
        END;
        """,
    ),
    (
        4,
        "PIT-safe identity history and recoverable snapshot publication",
        """
        ALTER TABLE security_identity_event RENAME TO security_identity_event_v3;
        CREATE TABLE security_identity_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            security_id TEXT NOT NULL REFERENCES security(security_id),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'baseline','ticker_change','share_class_change','corporate_action','delisting'
            )),
            old_value TEXT, new_value TEXT, event_time TEXT NOT NULL,
            public_available_time TEXT,
            pat_provenance TEXT NOT NULL CHECK (pat_provenance IN (
                'source_reported','derived_from_index','observed_at_ingest','unknown'
            )),
            ingested_time TEXT NOT NULL,
            supersedes_id INTEGER REFERENCES security_identity_event(id),
            CHECK (public_available_time IS NOT NULL OR pat_provenance IN (
                'unknown','observed_at_ingest'
            )),
            CHECK (public_available_time IS NULL OR public_available_time <= ingested_time)
        );
        INSERT INTO security_identity_event(
            id,security_id,event_type,old_value,new_value,event_time,
            public_available_time,pat_provenance,ingested_time
        ) SELECT id,security_id,event_type,old_value,new_value,event_time,
            public_available_time,pat_provenance,
            COALESCE(public_available_time,event_time) FROM security_identity_event_v3;
        DROP TABLE security_identity_event_v3;
        CREATE INDEX security_identity_event_idx ON security_identity_event(
            security_id,event_type,public_available_time
        );
        CREATE UNIQUE INDEX identity_single_successor_uq
            ON security_identity_event(supersedes_id) WHERE supersedes_id IS NOT NULL;
        CREATE INDEX identity_supersedes_idx ON security_identity_event(supersedes_id);
        CREATE TRIGGER identity_no_update BEFORE UPDATE ON security_identity_event BEGIN
            SELECT RAISE(ABORT, 'security_identity_event is append-only'); END;
        CREATE TRIGGER identity_no_delete BEFORE DELETE ON security_identity_event BEGIN
            SELECT RAISE(ABORT, 'security_identity_event is append-only'); END;
        CREATE TRIGGER identity_supersession_valid BEFORE INSERT ON security_identity_event
        WHEN NEW.supersedes_id IS NOT NULL BEGIN
            SELECT CASE WHEN NEW.supersedes_id = NEW.id
                THEN RAISE(ABORT, 'identity event cannot supersede itself') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND p.security_id=NEW.security_id
            ) THEN RAISE(ABORT, 'identity supersession requires same security') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND NEW.public_available_time < p.public_available_time
            ) THEN RAISE(ABORT, 'identity supersession cannot backdate knowledge time') END;
        END;

        ALTER TABLE snapshot_version ADD COLUMN status TEXT NOT NULL DEFAULT 'READY'
            CHECK (status IN ('PENDING','READY'));
        ALTER TABLE snapshot_version ADD COLUMN destination_path TEXT;
        """,
    ),
    (
        5,
        "Enforce compatible identity supersession domains",
        """
        DROP TRIGGER identity_supersession_valid;
        CREATE TRIGGER identity_supersession_valid BEFORE INSERT ON security_identity_event
        WHEN NEW.supersedes_id IS NOT NULL BEGIN
            SELECT CASE WHEN NEW.supersedes_id = NEW.id
                THEN RAISE(ABORT, 'identity event cannot supersede itself') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND p.security_id=NEW.security_id
            ) THEN RAISE(ABORT, 'identity supersession requires same security') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND NOT (
                        p.event_type=NEW.event_type OR
                        (p.event_type IN ('baseline','ticker_change') AND
                         NEW.event_type IN ('baseline','ticker_change'))
                    )
            ) THEN RAISE(ABORT, 'identity supersession requires compatible event domain') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND NEW.public_available_time < p.public_available_time
            ) THEN RAISE(ABORT, 'identity supersession cannot backdate knowledge time') END;
        END;
        """,
    ),
)

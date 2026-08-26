from __future__ import annotations

# ruff: noqa: E501 -- migration SQL remains legible as exact DDL statements.

PHASE_0_SCHEMA_VERSION = 11

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
            SELECT CASE WHEN NEW.public_available_time IS NULL OR EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND p.public_available_time IS NOT NULL
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
            SELECT CASE WHEN NEW.public_available_time IS NULL OR EXISTS (
                SELECT 1 FROM security_identity_event p WHERE p.id=NEW.supersedes_id
                    AND p.public_available_time IS NOT NULL
                    AND NEW.public_available_time < p.public_available_time
            ) THEN RAISE(ABORT, 'identity supersession cannot backdate knowledge time') END;
        END;
        """,
    ),
    (
        6,
        "Phase 1 screening operational ledger",
        """
        CREATE TABLE screen_definition (
            config_hash TEXT PRIMARY KEY,
            family TEXT NOT NULL CHECK (family IN (
                'valuation','inflection','quality','informed_activity','event',
                'momentum_confirmation'
            )),
            screen_id TEXT NOT NULL,
            screen_version INTEGER NOT NULL CHECK (screen_version > 0),
            spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
            created_at TEXT NOT NULL,
            UNIQUE (family, screen_id, screen_version)
        );

        CREATE TABLE pipeline_run (
            run_id TEXT PRIMARY KEY,
            as_of TEXT NOT NULL,
            universe_hash TEXT NOT NULL,
            screen_manifest_json TEXT NOT NULL CHECK (json_valid(screen_manifest_json)),
            screen_manifest_hash TEXT NOT NULL,
            funnel_config_json TEXT NOT NULL CHECK (json_valid(funnel_config_json)),
            funnel_config_hash TEXT NOT NULL,
            input_snapshot_id TEXT REFERENCES snapshot_version(snapshot_id),
            input_view_hash TEXT NOT NULL,
            expected_security_count INTEGER NOT NULL CHECK (expected_security_count >= 0),
            status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETE','FAILED')),
            failure_json TEXT CHECK (failure_json IS NULL OR json_valid(failure_json)),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            CHECK ((status='RUNNING' AND finished_at IS NULL) OR
                   (status IN ('COMPLETE','FAILED') AND finished_at IS NOT NULL))
        );

        CREATE TABLE screen_result (
            screen_result_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            config_hash TEXT NOT NULL REFERENCES screen_definition(config_hash),
            raw_features_json TEXT NOT NULL CHECK (json_valid(raw_features_json)),
            evidence_ids_json TEXT NOT NULL CHECK (json_valid(evidence_ids_json)),
            reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
            sufficient_data INTEGER NOT NULL CHECK (sufficient_data IN (0,1)),
            passed INTEGER NOT NULL CHECK (passed IN (0,1)),
            confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            data_quality REAL NOT NULL CHECK (data_quality BETWEEN 0 AND 1),
            result_hash TEXT NOT NULL,
            computed_at TEXT NOT NULL,
            CHECK (passed=0 OR sufficient_data=1),
            UNIQUE (run_id, security_id, config_hash)
        );

        CREATE TABLE candidate (
            candidate_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            inclusion_reasons_json TEXT NOT NULL CHECK (json_valid(inclusion_reasons_json)),
            screen_result_ids_json TEXT NOT NULL CHECK (json_valid(screen_result_ids_json)),
            rank_telemetry_json TEXT NOT NULL CHECK (json_valid(rank_telemetry_json)),
            is_control INTEGER NOT NULL CHECK (is_control IN (0,1)),
            control_algorithm TEXT,
            control_key TEXT,
            control_rank INTEGER CHECK (control_rank IS NULL OR control_rank > 0),
            included_at TEXT NOT NULL,
            UNIQUE (run_id, security_id),
            UNIQUE (run_id, ordinal),
            CHECK ((is_control=1 AND control_algorithm IS NOT NULL AND control_key IS NOT NULL
                    AND control_rank IS NOT NULL) OR
                   (is_control=0 AND control_algorithm IS NULL AND control_key IS NULL
                    AND control_rank IS NULL))
        );

        CREATE INDEX pipeline_run_status_idx ON pipeline_run(status, as_of);
        CREATE INDEX screen_result_population_idx
            ON screen_result(run_id, config_hash, sufficient_data, passed, security_id);
        CREATE INDEX screen_result_security_idx ON screen_result(security_id, run_id);
        CREATE INDEX candidate_run_idx ON candidate(run_id, ordinal);
        CREATE INDEX evidence_kind_pit_idx ON evidence_event(
            security_id, json_extract(structured_fields,'$.record_type'), public_available_time
        );
        CREATE INDEX evidence_form4_pit_idx ON evidence_event(
            security_id, json_extract(structured_fields,'$.transaction_code'),
            public_available_time
        ) WHERE json_extract(structured_fields,'$.record_type')='form4_transaction';

        CREATE TRIGGER pipeline_run_immutable_inputs BEFORE UPDATE ON pipeline_run
        WHEN OLD.run_id IS NOT NEW.run_id
          OR OLD.as_of IS NOT NEW.as_of
          OR OLD.universe_hash IS NOT NEW.universe_hash
          OR OLD.screen_manifest_json IS NOT NEW.screen_manifest_json
          OR OLD.screen_manifest_hash IS NOT NEW.screen_manifest_hash
          OR OLD.funnel_config_json IS NOT NEW.funnel_config_json
          OR OLD.funnel_config_hash IS NOT NEW.funnel_config_hash
          OR OLD.input_snapshot_id IS NOT NEW.input_snapshot_id
          OR OLD.input_view_hash IS NOT NEW.input_view_hash
          OR OLD.expected_security_count IS NOT NEW.expected_security_count
        BEGIN SELECT RAISE(ABORT, 'pipeline_run logical inputs are immutable'); END;

        CREATE TRIGGER screen_result_run_complete BEFORE INSERT ON screen_result
        WHEN (SELECT status FROM pipeline_run WHERE run_id=NEW.run_id)='COMPLETE'
        BEGIN SELECT RAISE(ABORT, 'completed pipeline_run is immutable'); END;
        CREATE TRIGGER candidate_run_complete BEFORE INSERT ON candidate
        WHEN (SELECT status FROM pipeline_run WHERE run_id=NEW.run_id)='COMPLETE'
        BEGIN SELECT RAISE(ABORT, 'completed pipeline_run is immutable'); END;
        CREATE TRIGGER screen_result_no_update BEFORE UPDATE ON screen_result BEGIN
            SELECT RAISE(ABORT, 'screen_result is append-only'); END;
        CREATE TRIGGER screen_result_no_delete BEFORE DELETE ON screen_result BEGIN
            SELECT RAISE(ABORT, 'screen_result is append-only'); END;
        CREATE TRIGGER candidate_no_update BEFORE UPDATE ON candidate BEGIN
            SELECT RAISE(ABORT, 'candidate is append-only'); END;
        CREATE TRIGGER candidate_no_delete BEFORE DELETE ON candidate BEGIN
            SELECT RAISE(ABORT, 'candidate is append-only'); END;
        """,
    ),
    (
        7,
        "Phase 1 reviewed recovery flags and provider operational limits",
        """
        DROP TRIGGER candidate_run_complete;
        ALTER TABLE pipeline_run ADD COLUMN flags_json TEXT
            CHECK (flags_json IS NULL OR json_valid(flags_json));
        CREATE TABLE provider_request_event (
            provider TEXT NOT NULL,
            requested_at REAL NOT NULL
        );
        CREATE INDEX provider_request_event_window_idx
            ON provider_request_event(provider, requested_at);
        CREATE TABLE provider_bootstrap_symbol (
            provider TEXT NOT NULL,
            symbol TEXT NOT NULL,
            first_requested_at REAL NOT NULL,
            PRIMARY KEY(provider, symbol)
        );
        CREATE INDEX provider_bootstrap_symbol_window_idx
            ON provider_bootstrap_symbol(provider, first_requested_at);
        DROP INDEX evidence_kind_pit_idx;
        DROP INDEX evidence_form4_pit_idx;
        """,
    ),
    (
        8,
        "Phase 2 evidence packs and committee artifacts",
        """
        CREATE TABLE comparator_definition (
            config_hash TEXT PRIMARY KEY,
            comparator_version INTEGER NOT NULL UNIQUE CHECK(comparator_version > 0),
            taxonomy_version INTEGER NOT NULL CHECK(taxonomy_version > 0),
            spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
            created_at TEXT NOT NULL
        );
        CREATE TABLE scoring_version (
            config_hash TEXT PRIMARY KEY,
            scoring_version INTEGER NOT NULL UNIQUE CHECK(scoring_version > 0),
            spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE evidence_pack (
            pack_hash TEXT PRIMARY KEY,
            pack_spec_version INTEGER NOT NULL CHECK(pack_spec_version > 0),
            candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
            pipeline_run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
            body_json TEXT NOT NULL CHECK(json_valid(body_json)),
            body_chars INTEGER NOT NULL CHECK(body_chars >= 0),
            built_at TEXT NOT NULL,
            UNIQUE(candidate_id, pack_spec_version)
        );
        CREATE TABLE committee_run (
            committee_run_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
            pipeline_run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
            pack_hash TEXT NOT NULL REFERENCES evidence_pack(pack_hash),
            role_set_json TEXT NOT NULL CHECK(json_valid(role_set_json)),
            committee_policy_version INTEGER NOT NULL CHECK(committee_policy_version > 0),
            comparator_config_hash TEXT NOT NULL REFERENCES comparator_definition(config_hash),
            scoring_config_hash TEXT NOT NULL REFERENCES scoring_version(config_hash),
            prompt_versions_json TEXT NOT NULL CHECK(json_valid(prompt_versions_json)),
            assessment_schema_version INTEGER NOT NULL CHECK(assessment_schema_version > 0),
            created_at TEXT NOT NULL
        );
        CREATE TABLE committee_transition (
            transition_id TEXT PRIMARY KEY,
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            from_state TEXT CHECK(from_state IS NULL OR from_state IN (
                'PENDING_NEUTRALS','RED_TEAM_REQUIRED','ARBITER_REQUIRED','READY_TO_SCORE',
                'SCORED','BLOCKED','ESCALATE')),
            to_state TEXT NOT NULL CHECK(to_state IN (
                'PENDING_NEUTRALS','RED_TEAM_REQUIRED','ARBITER_REQUIRED','READY_TO_SCORE',
                'SCORED','BLOCKED','ESCALATE')),
            cause_code TEXT NOT NULL,
            artifact_id TEXT,
            occurred_at TEXT NOT NULL,
            UNIQUE(committee_run_id, from_state, to_state, cause_code, artifact_id)
        );
        CREATE TABLE model_assessment (
            assessment_id TEXT PRIMARY KEY,
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
            pack_hash TEXT NOT NULL REFERENCES evidence_pack(pack_hash),
            role TEXT NOT NULL CHECK(role IN (
                'neutral_analyst_a','neutral_analyst_b','red_team','arbiter')),
            provider TEXT NOT NULL, model_id TEXT NOT NULL, prompt_version TEXT NOT NULL,
            assessment_schema_version INTEGER NOT NULL CHECK(assessment_schema_version > 0),
            taxonomy_version INTEGER NOT NULL CHECK(taxonomy_version > 0),
            model_route TEXT NOT NULL,
            billing_class TEXT NOT NULL CHECK(billing_class IN ('subscription','local','paid')),
            claims_json TEXT NOT NULL CHECK(json_valid(claims_json)),
            cited_evidence_ids_json TEXT NOT NULL CHECK(json_valid(cited_evidence_ids_json)),
            missing_evidence_json TEXT NOT NULL CHECK(json_valid(missing_evidence_json)),
            thesis_json TEXT NOT NULL CHECK(json_valid(thesis_json)),
            confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
            uncertainty REAL NOT NULL CHECK(uncertainty BETWEEN 0 AND 1),
            usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),
            cost_json TEXT NOT NULL CHECK(json_valid(cost_json)),
            evaluation_time TEXT NOT NULL, submitted_at TEXT NOT NULL, payload_hash TEXT NOT NULL,
            semantic_assessment_hash TEXT NOT NULL,
            UNIQUE(committee_run_id, role)
        );
        CREATE TABLE committee_work (
            work_id TEXT PRIMARY KEY,
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            role TEXT NOT NULL CHECK(role IN (
                'neutral_analyst_a','neutral_analyst_b','red_team','arbiter')),
            attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 2),
            pack_hash TEXT NOT NULL REFERENCES evidence_pack(pack_hash),
            prompt_version TEXT NOT NULL,
            assessment_schema_version INTEGER NOT NULL CHECK(assessment_schema_version > 0),
            taxonomy_version INTEGER NOT NULL CHECK(taxonomy_version > 0),
            focus_hash TEXT,
            focus_json TEXT CHECK(focus_json IS NULL OR json_valid(focus_json)),
            issued_at TEXT NOT NULL,
            UNIQUE(committee_run_id, role, attempt_number)
        );
        CREATE TABLE model_call_attempt (
            attempt_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES committee_work(work_id),
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            role TEXT NOT NULL CHECK(role IN (
                'neutral_analyst_a','neutral_analyst_b','red_team','arbiter')),
            attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 2),
            provider TEXT NOT NULL, model_id TEXT NOT NULL, model_route TEXT NOT NULL,
            billing_class TEXT NOT NULL CHECK(billing_class IN ('subscription','local','paid')),
            prompt_version TEXT NOT NULL, prompt_template_hash TEXT NOT NULL,
            pack_hash TEXT NOT NULL REFERENCES evidence_pack(pack_hash),
            outcome TEXT NOT NULL CHECK(outcome IN (
                'accepted','malformed','unavailable','timeout')),
            usage_json TEXT NOT NULL CHECK(json_valid(usage_json)),
            cost_json TEXT NOT NULL CHECK(json_valid(cost_json)), diagnostic_hash TEXT,
            diagnostic_excerpt TEXT, requested_at TEXT NOT NULL, completed_at TEXT,
            UNIQUE(committee_run_id, role, attempt_number)
        );
        CREATE TABLE comparison_report (
            comparison_id TEXT PRIMARY KEY,
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            assessment_id_a TEXT NOT NULL REFERENCES model_assessment(assessment_id),
            assessment_id_b TEXT NOT NULL REFERENCES model_assessment(assessment_id),
            comparator_config_hash TEXT NOT NULL REFERENCES comparator_definition(config_hash),
            report_json TEXT NOT NULL CHECK(json_valid(report_json)),
            agreement REAL CHECK(agreement IS NULL OR agreement BETWEEN 0 AND 1),
            routing_decision TEXT NOT NULL, result_hash TEXT NOT NULL, computed_at TEXT NOT NULL,
            UNIQUE(committee_run_id, assessment_id_a, assessment_id_b, comparator_config_hash)
        );
        CREATE TABLE dispute_resolution (
            resolution_id TEXT PRIMARY KEY,
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            comparison_id TEXT NOT NULL REFERENCES comparison_report(comparison_id),
            role TEXT NOT NULL CHECK(role IN ('red_team','arbiter')),
            assessment_id TEXT NOT NULL REFERENCES model_assessment(assessment_id),
            focus_hash TEXT NOT NULL, focus_json TEXT NOT NULL CHECK(json_valid(focus_json)),
            resolution_json TEXT NOT NULL CHECK(json_valid(resolution_json)),
            result_hash TEXT NOT NULL, computed_at TEXT NOT NULL,
            UNIQUE(comparison_id, role)
        );
        CREATE TABLE score_snapshot (
            snapshot_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
            committee_run_id TEXT NOT NULL REFERENCES committee_run(committee_run_id),
            scoring_config_hash TEXT NOT NULL REFERENCES scoring_version(config_hash),
            score_input_hash TEXT NOT NULL UNIQUE, scored_evidence_hash TEXT NOT NULL,
            assessment_ids_json TEXT NOT NULL CHECK(json_valid(assessment_ids_json)),
            comparison_id TEXT NOT NULL REFERENCES comparison_report(comparison_id),
            resolution_ids_json TEXT NOT NULL CHECK(json_valid(resolution_ids_json)),
            family_contributions_json TEXT NOT NULL CHECK(json_valid(family_contributions_json)),
            underlying_groups_json TEXT NOT NULL CHECK(json_valid(underlying_groups_json)),
            penalties_json TEXT NOT NULL CHECK(json_valid(penalties_json)),
            base_evidence REAL NOT NULL, confluence_bonus REAL NOT NULL, raw_score REAL NOT NULL,
            conviction INTEGER NOT NULL CHECK(conviction BETWEEN 0 AND 100 AND conviction % 5 = 0),
            data_quality REAL NOT NULL CHECK(data_quality BETWEEN 0 AND 1),
            committee_agreement REAL CHECK(committee_agreement IS NULL OR committee_agreement BETWEEN 0 AND 1),
            prior_snapshot_id TEXT REFERENCES score_snapshot(snapshot_id), prior_conviction INTEGER,
            conviction_delta INTEGER, trajectory_label TEXT NOT NULL CHECK(trajectory_label IN (
                'INITIAL','REBASED','RISING','FALLING','STABLE')),
            change_cause TEXT NOT NULL CHECK(change_cause IN (
                'INITIAL','SCORING_VERSION_CHANGE','MODEL_REASSESSMENT',
                'SCREEN_METHODOLOGY_CHANGE','CORRECTION_RESTATEMENT','EVIDENCE_DRIVEN')),
            material_change_time TEXT,
            reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json)),
            result_hash TEXT NOT NULL, computed_at TEXT NOT NULL
        );
        CREATE INDEX committee_candidate_idx ON committee_run(candidate_id, created_at);
        CREATE INDEX assessment_run_role_idx ON model_assessment(committee_run_id, role);
        CREATE INDEX work_run_role_idx ON committee_work(committee_run_id, role, attempt_number);
        CREATE INDEX attempt_run_role_idx ON model_call_attempt(committee_run_id, role, attempt_number);
        CREATE INDEX score_candidate_idx ON score_snapshot(candidate_id, material_change_time, snapshot_id);

        CREATE TRIGGER comparator_definition_no_update BEFORE UPDATE ON comparator_definition BEGIN SELECT RAISE(ABORT, 'comparator_definition is append-only'); END;
        CREATE TRIGGER comparator_definition_no_delete BEFORE DELETE ON comparator_definition BEGIN SELECT RAISE(ABORT, 'comparator_definition is append-only'); END;
        CREATE TRIGGER scoring_version_no_update BEFORE UPDATE ON scoring_version BEGIN SELECT RAISE(ABORT, 'scoring_version is append-only'); END;
        CREATE TRIGGER scoring_version_no_delete BEFORE DELETE ON scoring_version BEGIN SELECT RAISE(ABORT, 'scoring_version is append-only'); END;
        CREATE TRIGGER evidence_pack_no_update BEFORE UPDATE ON evidence_pack BEGIN SELECT RAISE(ABORT, 'evidence_pack is append-only'); END;
        CREATE TRIGGER evidence_pack_no_delete BEFORE DELETE ON evidence_pack BEGIN SELECT RAISE(ABORT, 'evidence_pack is append-only'); END;
        CREATE TRIGGER committee_run_no_update BEFORE UPDATE ON committee_run BEGIN SELECT RAISE(ABORT, 'committee_run is append-only'); END;
        CREATE TRIGGER committee_run_no_delete BEFORE DELETE ON committee_run BEGIN SELECT RAISE(ABORT, 'committee_run is append-only'); END;
        CREATE TRIGGER committee_transition_no_update BEFORE UPDATE ON committee_transition BEGIN SELECT RAISE(ABORT, 'committee_transition is append-only'); END;
        CREATE TRIGGER committee_transition_no_delete BEFORE DELETE ON committee_transition BEGIN SELECT RAISE(ABORT, 'committee_transition is append-only'); END;
        CREATE TRIGGER model_assessment_no_update BEFORE UPDATE ON model_assessment BEGIN SELECT RAISE(ABORT, 'model_assessment is append-only'); END;
        CREATE TRIGGER model_assessment_no_delete BEFORE DELETE ON model_assessment BEGIN SELECT RAISE(ABORT, 'model_assessment is append-only'); END;
        CREATE TRIGGER committee_work_no_update BEFORE UPDATE ON committee_work BEGIN SELECT RAISE(ABORT, 'committee_work is append-only'); END;
        CREATE TRIGGER committee_work_no_delete BEFORE DELETE ON committee_work BEGIN SELECT RAISE(ABORT, 'committee_work is append-only'); END;
        CREATE TRIGGER model_call_attempt_no_update BEFORE UPDATE ON model_call_attempt BEGIN SELECT RAISE(ABORT, 'model_call_attempt is append-only'); END;
        CREATE TRIGGER model_call_attempt_no_delete BEFORE DELETE ON model_call_attempt BEGIN SELECT RAISE(ABORT, 'model_call_attempt is append-only'); END;
        CREATE TRIGGER comparison_report_no_update BEFORE UPDATE ON comparison_report BEGIN SELECT RAISE(ABORT, 'comparison_report is append-only'); END;
        CREATE TRIGGER comparison_report_no_delete BEFORE DELETE ON comparison_report BEGIN SELECT RAISE(ABORT, 'comparison_report is append-only'); END;
        CREATE TRIGGER dispute_resolution_no_update BEFORE UPDATE ON dispute_resolution BEGIN SELECT RAISE(ABORT, 'dispute_resolution is append-only'); END;
        CREATE TRIGGER dispute_resolution_no_delete BEFORE DELETE ON dispute_resolution BEGIN SELECT RAISE(ABORT, 'dispute_resolution is append-only'); END;
        CREATE TRIGGER score_snapshot_no_update BEFORE UPDATE ON score_snapshot BEGIN SELECT RAISE(ABORT, 'score_snapshot is append-only'); END;
        CREATE TRIGGER score_snapshot_no_delete BEFORE DELETE ON score_snapshot BEGIN SELECT RAISE(ABORT, 'score_snapshot is append-only'); END;
        """,
    ),
    (
        9,
        "Phase 2 committee provider-independence invariant",
        """
        CREATE TRIGGER neutral_provider_independence
        BEFORE INSERT ON model_assessment
        WHEN NEW.role IN ('neutral_analyst_a','neutral_analyst_b')
          AND EXISTS (
            SELECT 1 FROM model_assessment existing
            WHERE existing.committee_run_id = NEW.committee_run_id
              AND existing.role IN ('neutral_analyst_a','neutral_analyst_b')
              AND existing.role <> NEW.role
              AND existing.provider = NEW.provider
          )
        BEGIN
          SELECT RAISE(ABORT, 'neutral providers must differ');
        END;
        """,
    ),
    (
        10,
        "Phase 3 portfolio state machine, policy registry, and paper proposals",
        """
        CREATE TABLE portfolio_policy (
            policy_version TEXT PRIMARY KEY CHECK(length(trim(policy_version))>0),
            policy_status TEXT NOT NULL CHECK(policy_status IN ('FIXTURE','PROVISIONAL','PAPER')),
            sizing_policy_version TEXT NOT NULL CHECK(length(trim(sizing_policy_version))>0),
            spec_json TEXT NOT NULL CHECK(json_valid(spec_json) AND json_type(spec_json)='object'),
            spec_hash TEXT NOT NULL UNIQUE CHECK(length(spec_hash)=64),
            approved_by TEXT, approved_at TEXT, created_at TEXT NOT NULL,
            -- composite UNIQUE is the FK target for trade_proposal; policy_version PK
            -- uniqueness makes it trivially satisfied for the registry itself.
            UNIQUE(policy_version, sizing_policy_version),
            CHECK(
                (policy_status='PAPER' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
                OR (policy_status IN ('FIXTURE','PROVISIONAL') AND approved_by IS NULL AND approved_at IS NULL)
            )
        );
        CREATE TABLE portfolio_snapshot (
            snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id)=64),
            as_of TEXT NOT NULL,
            currency TEXT NOT NULL CHECK(currency='USD'),
            cash_microusd INTEGER, cash_status TEXT NOT NULL CHECK(cash_status IN ('KNOWN','STALE','UNKNOWN')),
            nav_microusd INTEGER, valuation_status TEXT NOT NULL CHECK(valuation_status IN ('KNOWN','STALE','UNKNOWN')),
            holdings_status TEXT NOT NULL CHECK(holdings_status IN ('KNOWN','STALE','UNKNOWN')),
            provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json) AND json_type(provenance_json)='object'),
            input_hash TEXT NOT NULL UNIQUE CHECK(length(input_hash)=64),
            recorded_at TEXT NOT NULL,
            CHECK(cash_microusd IS NULL OR cash_microusd>=0),
            CHECK(nav_microusd IS NULL OR nav_microusd>0),
            CHECK((cash_status='UNKNOWN')=(cash_microusd IS NULL)),
            CHECK((valuation_status='UNKNOWN')=(nav_microusd IS NULL))
        );
        CREATE TABLE portfolio_holding (
            snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshot(snapshot_id),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            quantity_microunits INTEGER NOT NULL CHECK(quantity_microunits>=0),
            sellable_quantity_microunits INTEGER,
            sellable_status TEXT NOT NULL CHECK(sellable_status IN ('KNOWN','STALE','UNKNOWN')),
            market_value_microusd INTEGER,
            valuation_status TEXT NOT NULL CHECK(valuation_status IN ('KNOWN','STALE','UNKNOWN')),
            sector TEXT, sector_status TEXT NOT NULL CHECK(sector_status IN ('KNOWN','UNKNOWN')),
            provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json) AND json_type(provenance_json)='object'),
            PRIMARY KEY (snapshot_id, security_id),
            CHECK(sellable_quantity_microunits IS NULL OR sellable_quantity_microunits BETWEEN 0 AND quantity_microunits),
            CHECK((sellable_status='UNKNOWN')=(sellable_quantity_microunits IS NULL)),
            CHECK(market_value_microusd IS NULL OR market_value_microusd>=0),
            CHECK((valuation_status='UNKNOWN')=(market_value_microusd IS NULL)),
            CHECK((sector_status='UNKNOWN')=(sector IS NULL)),
            CHECK(sector IS NULL OR length(trim(sector))>0)
        );
        CREATE TABLE portfolio_market_input (
            snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshot(snapshot_id),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            mark_price_microusd INTEGER, price_as_of TEXT, price_status TEXT NOT NULL CHECK(price_status IN ('KNOWN','STALE','UNKNOWN')),
            avg_dollar_volume_microusd INTEGER, liquidity_as_of TEXT, liquidity_status TEXT NOT NULL CHECK(liquidity_status IN ('KNOWN','STALE','UNKNOWN')),
            evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json) AND json_type(evidence_ids_json)='array'),
            PRIMARY KEY (snapshot_id, security_id),
            CHECK(mark_price_microusd IS NULL OR mark_price_microusd>0),
            CHECK((price_status='UNKNOWN')=(mark_price_microusd IS NULL)),
            CHECK((price_status='UNKNOWN')=(price_as_of IS NULL)),
            CHECK(avg_dollar_volume_microusd IS NULL OR avg_dollar_volume_microusd>=0),
            CHECK((liquidity_status='UNKNOWN')=(avg_dollar_volume_microusd IS NULL)),
            CHECK((liquidity_status='UNKNOWN')=(liquidity_as_of IS NULL))
        );
        CREATE TABLE portfolio_signal_input (
            signal_input_id TEXT PRIMARY KEY CHECK(length(signal_input_id)=64),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            as_of TEXT NOT NULL,
            remaining_opportunity_ppm INTEGER,
            opportunity_status TEXT NOT NULL CHECK(opportunity_status IN ('KNOWN','UNKNOWN')),
            source_kind TEXT NOT NULL CHECK(source_kind IN ('FIXTURE','COMMITTEE_STRUCTURED','DETERMINISTIC_METRIC','OWNER_ATTESTED')),
            evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json) AND json_type(evidence_ids_json)='array'),
            input_hash TEXT NOT NULL UNIQUE CHECK(length(input_hash)=64),
            recorded_at TEXT NOT NULL,
            CHECK(remaining_opportunity_ppm IS NULL OR remaining_opportunity_ppm BETWEEN 0 AND 1000000),
            CHECK((opportunity_status='UNKNOWN')=(remaining_opportunity_ppm IS NULL))
        );
        CREATE TABLE thesis_break_event (
            event_id TEXT PRIMARY KEY CHECK(length(event_id)=64),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            condition_id TEXT NOT NULL CHECK(length(trim(condition_id))>0),
            condition_text TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json) AND json_type(evidence_ids_json)='array' AND json_array_length(evidence_ids_json)>0),
            detection_score_snapshot_id TEXT NOT NULL REFERENCES score_snapshot(snapshot_id),
            detected_at TEXT NOT NULL,
            input_hash TEXT NOT NULL UNIQUE CHECK(length(input_hash)=64),
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE thesis_break_verification (
            verification_id TEXT PRIMARY KEY CHECK(length(verification_id)=64),
            event_id TEXT NOT NULL REFERENCES thesis_break_event(event_id),
            status TEXT NOT NULL CHECK(status IN ('VERIFIED','REJECTED')),
            verification_method TEXT NOT NULL CHECK(verification_method IN ('OWNER_ATTESTED','DETERMINISTIC_RULE','FIXTURE')),
            verified_at TEXT NOT NULL,
            score_snapshot_id TEXT NOT NULL REFERENCES score_snapshot(snapshot_id),
            evidence_ids_json TEXT NOT NULL CHECK(json_valid(evidence_ids_json) AND json_type(evidence_ids_json)='array' AND json_array_length(evidence_ids_json)>0),
            verifier_ref TEXT NOT NULL CHECK(length(trim(verifier_ref))>0),
            input_hash TEXT NOT NULL UNIQUE CHECK(length(input_hash)=64),
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE portfolio_run (
            run_id TEXT PRIMARY KEY CHECK(length(run_id)=64),
            pipeline_run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
            decision_as_of TEXT NOT NULL,
            portfolio_snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshot(snapshot_id),
            policy_version TEXT NOT NULL REFERENCES portfolio_policy(policy_version),
            score_set_hash TEXT NOT NULL CHECK(length(score_set_hash)=64),
            signal_set_hash TEXT NOT NULL CHECK(length(signal_set_hash)=64),
            candidate_set_hash TEXT NOT NULL CHECK(length(candidate_set_hash)=64),
            invocation_key TEXT NOT NULL UNIQUE CHECK(length(invocation_key)=64),
            state_prestate_hash TEXT NOT NULL CHECK(length(state_prestate_hash)=64),
            market_data_prestate_hash TEXT NOT NULL CHECK(length(market_data_prestate_hash)=64),
            budget_prestate_hash TEXT NOT NULL CHECK(length(budget_prestate_hash)=64),
            input_hash TEXT NOT NULL UNIQUE CHECK(length(input_hash)=64),
            expected_security_count INTEGER NOT NULL CHECK(expected_security_count>=0),
            created_at TEXT NOT NULL
        );
        CREATE TABLE portfolio_state_observation (
            decision_id TEXT PRIMARY KEY CHECK(length(decision_id)=64),
            run_id TEXT NOT NULL REFERENCES portfolio_run(run_id),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            current_state TEXT NOT NULL CHECK(current_state IN ('DISCOVER','WATCH','ENTER','HOLD','ADD','TRIM','EXIT')),
            signal_state TEXT NOT NULL CHECK(signal_state IN ('DISCOVER','WATCH','ENTER','HOLD','ADD','TRIM','EXIT')),
            proposed_state TEXT NOT NULL CHECK(proposed_state IN ('DISCOVER','WATCH','ENTER','HOLD','ADD','TRIM','EXIT')),
            score_snapshot_id TEXT REFERENCES score_snapshot(snapshot_id),
            signal_input_id TEXT REFERENCES portfolio_signal_input(signal_input_id),
            portfolio_snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshot(snapshot_id),
            policy_version TEXT NOT NULL REFERENCES portfolio_policy(policy_version),
            scored_evidence_hash TEXT,
            change_cause TEXT CHECK(change_cause IS NULL OR change_cause IN (
                'INITIAL','EVIDENCE_DRIVEN','CORRECTION_RESTATEMENT','SCORING_VERSION_CHANGE',
                'SCREEN_METHODOLOGY_CHANGE','MODEL_REASSESSMENT')),
            evidence_driven INTEGER NOT NULL CHECK(evidence_driven IN (0,1)),
            signal_status TEXT NOT NULL CHECK(signal_status IN ('PASS','INELIGIBLE','UNKNOWN','BLOCKED')),
            persistence_count_at_decision INTEGER NOT NULL CHECK(persistence_count_at_decision>=0),
            persistence_required INTEGER NOT NULL CHECK(persistence_required>=0),
            material_change_satisfied INTEGER NOT NULL CHECK(material_change_satisfied IN (0,1)),
            cooldown_satisfied INTEGER NOT NULL CHECK(cooldown_satisfied IN (0,1)),
            risk_status TEXT NOT NULL CHECK(risk_status IN ('PASS','LIMITED','UNKNOWN','BLOCKED','NOT_RUN')),
            final_status TEXT NOT NULL CHECK(final_status IN ('TRANSITIONED','PROPOSED','NO_ACTION','BLOCKED')),
            reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json) AND json_type(reason_codes_json)='array'),
            risk_json TEXT NOT NULL CHECK(json_valid(risk_json) AND json_type(risk_json)='object'),
            sizing_json TEXT NOT NULL CHECK(json_valid(sizing_json) AND json_type(sizing_json)='object'),
            decision_input_hash TEXT NOT NULL CHECK(length(decision_input_hash)=64),
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE portfolio_state_transition (
            transition_id TEXT PRIMARY KEY CHECK(length(transition_id)=64),
            decision_id TEXT NOT NULL UNIQUE REFERENCES portfolio_state_observation(decision_id),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            from_state TEXT NOT NULL CHECK(from_state IN ('DISCOVER','WATCH','ENTER','HOLD','ADD','TRIM','EXIT')),
            to_state TEXT NOT NULL CHECK(to_state IN ('DISCOVER','WATCH','ENTER','HOLD','ADD','TRIM','EXIT')),
            cause TEXT NOT NULL CHECK(cause IN ('RULE_PERSISTED','MATERIAL_CHANGE','VERIFIED_THESIS_BREAK','SETTLEMENT','COOLDOWN')),
            reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json) AND json_type(reason_codes_json)='array'),
            score_snapshot_id TEXT NOT NULL REFERENCES score_snapshot(snapshot_id),
            portfolio_snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshot(snapshot_id),
            policy_version TEXT NOT NULL REFERENCES portfolio_policy(policy_version),
            thesis_break_verification_id TEXT REFERENCES thesis_break_verification(verification_id),
            persistence_count INTEGER NOT NULL CHECK(persistence_count>=0),
            persistence_required INTEGER NOT NULL CHECK(persistence_required>=0),
            effective_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            -- intentional single-writer semantics: at most one portfolio decision per
            -- security per effective moment; concurrent same-as_of runs are serialized.
            UNIQUE(security_id, effective_at),
            CHECK(
                (from_state='DISCOVER' AND to_state='WATCH')
                OR (from_state='WATCH' AND to_state IN ('DISCOVER','ENTER'))
                OR (from_state='ENTER' AND to_state='HOLD')
                OR (from_state='ADD' AND to_state='HOLD')
                OR (from_state='HOLD' AND to_state IN ('ADD','TRIM','EXIT'))
                OR (from_state='TRIM' AND to_state IN ('HOLD','EXIT'))
                OR (from_state='EXIT' AND to_state='WATCH')
            ),
            CHECK((cause='VERIFIED_THESIS_BREAK')=(thesis_break_verification_id IS NOT NULL))
        );
        CREATE TABLE portfolio_activity_day (
            activity_date TEXT PRIMARY KEY CHECK(length(activity_date)=10),
            policy_version TEXT NOT NULL REFERENCES portfolio_policy(policy_version),
            max_actionable_count INTEGER NOT NULL CHECK(max_actionable_count>=0),
            max_notional_microusd INTEGER NOT NULL CHECK(max_notional_microusd>=0),
            day_start_cash_microusd INTEGER CHECK(day_start_cash_microusd IS NULL OR day_start_cash_microusd>=0),
            input_hash TEXT NOT NULL UNIQUE CHECK(length(input_hash)=64),
            created_at TEXT NOT NULL
        );
        CREATE TABLE trade_proposal (
            proposal_id TEXT PRIMARY KEY CHECK(length(proposal_id)=64),
            decision_id TEXT NOT NULL UNIQUE REFERENCES portfolio_state_observation(decision_id),
            transition_id TEXT NOT NULL UNIQUE REFERENCES portfolio_state_transition(transition_id),
            activity_date TEXT NOT NULL REFERENCES portfolio_activity_day(activity_date),
            security_id TEXT NOT NULL REFERENCES security(security_id),
            current_state TEXT NOT NULL,
            proposed_state TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json) AND json_type(reason_codes_json)='array' AND json_array_length(reason_codes_json)>0),
            conviction_ppm INTEGER NOT NULL CHECK(conviction_ppm BETWEEN 0 AND 1000000),
            data_quality_ppm INTEGER NOT NULL CHECK(data_quality_ppm BETWEEN 0 AND 1000000),
            agreement_ppm INTEGER NOT NULL CHECK(agreement_ppm BETWEEN 0 AND 1000000),
            trajectory TEXT NOT NULL CHECK(trajectory IN ('INITIAL','RISING','FALLING','STABLE','REBASED')),
            current_weight_ppm INTEGER NOT NULL CHECK(current_weight_ppm BETWEEN 0 AND 1000000),
            target_weight_ppm INTEGER NOT NULL CHECK(target_weight_ppm BETWEEN 0 AND 1000000),
            max_quantity_microunits INTEGER NOT NULL CHECK(max_quantity_microunits>0),
            completion_quantity_microunits INTEGER NOT NULL CHECK(completion_quantity_microunits>=0),
            max_notional_microusd INTEGER NOT NULL CHECK(max_notional_microusd>0),
            order_constraints_json TEXT NOT NULL CHECK(json_valid(order_constraints_json) AND json_type(order_constraints_json)='object'),
            score_snapshot_id TEXT NOT NULL REFERENCES score_snapshot(snapshot_id),
            portfolio_snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshot(snapshot_id),
            policy_version TEXT NOT NULL,
            sizing_policy_version TEXT NOT NULL,
            proposal_mode TEXT NOT NULL CHECK(proposal_mode='PAPER'),
            requires_human_approval INTEGER NOT NULL CHECK(requires_human_approval=1),
            created_at TEXT NOT NULL,
            FOREIGN KEY(policy_version, sizing_policy_version)
                REFERENCES portfolio_policy(policy_version, sizing_policy_version),
            CHECK(
                (action='BUY' AND target_weight_ppm>current_weight_ppm
                 AND ((current_state='WATCH' AND proposed_state='ENTER')
                      OR (current_state='HOLD' AND proposed_state='ADD')))
                OR (action='SELL' AND target_weight_ppm<current_weight_ppm
                    AND ((current_state='HOLD' AND proposed_state IN ('TRIM','EXIT'))
                         OR (current_state='TRIM' AND proposed_state='EXIT')))
            )
        );
        CREATE TABLE portfolio_briefing (
            briefing_id TEXT PRIMARY KEY CHECK(length(briefing_id)=64),
            run_id TEXT NOT NULL REFERENCES portfolio_run(run_id),
            format_version TEXT NOT NULL CHECK(format_version='MWF_V1'),
            body_text TEXT NOT NULL,
            body_hash TEXT NOT NULL CHECK(length(body_hash)=64),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, format_version)
        );
        CREATE INDEX portfolio_obs_persistence_idx ON portfolio_state_observation(
            security_id, policy_version, evidence_driven, observed_at);
        CREATE INDEX portfolio_transition_security_idx ON portfolio_state_transition(
            security_id, effective_at);
        CREATE INDEX portfolio_proposal_date_idx ON trade_proposal(activity_date);
        CREATE TRIGGER trade_proposal_sell_reason_guard BEFORE INSERT ON trade_proposal
        WHEN NEW.action='SELL' AND EXISTS(
            SELECT 1 FROM json_each(NEW.reason_codes_json)
            WHERE value NOT IN ('thesis_broken','thesis_realised','opportunity_cost','risk_reduction','data_integrity','policy_ineligible')
        )
        BEGIN SELECT RAISE(ABORT, 'invalid SELL reason'); END;
        CREATE TRIGGER trade_proposal_sell_bounds_guard BEFORE INSERT ON trade_proposal
        WHEN NEW.action='SELL' AND (
            (NEW.completion_quantity_microunits <> 0 AND NEW.proposed_state='EXIT')
            OR EXISTS (
                SELECT 1 FROM portfolio_holding h LEFT JOIN portfolio_market_input m
                ON m.snapshot_id=h.snapshot_id AND m.security_id=h.security_id
                WHERE h.snapshot_id=NEW.portfolio_snapshot_id AND h.security_id=NEW.security_id
                  AND h.sellable_status='KNOWN'
                  AND (NEW.max_quantity_microunits > h.sellable_quantity_microunits
                       OR h.sellable_quantity_microunits > h.quantity_microunits
                       OR (m.price_status='KNOWN' AND NEW.max_notional_microusd >
                           NEW.max_quantity_microunits * m.mark_price_microusd / 1000000))
            )
        )
        BEGIN SELECT RAISE(ABORT, 'SELL proposal violates holdings bounds (sellable/exit completion/notional)'); END;
        CREATE TRIGGER portfolio_policy_no_update BEFORE UPDATE ON portfolio_policy BEGIN SELECT RAISE(ABORT, 'portfolio_policy is append-only'); END;
        CREATE TRIGGER portfolio_policy_no_delete BEFORE DELETE ON portfolio_policy BEGIN SELECT RAISE(ABORT, 'portfolio_policy is append-only'); END;
        CREATE TRIGGER portfolio_snapshot_no_update BEFORE UPDATE ON portfolio_snapshot BEGIN SELECT RAISE(ABORT, 'portfolio_snapshot is append-only'); END;
        CREATE TRIGGER portfolio_snapshot_no_delete BEFORE DELETE ON portfolio_snapshot BEGIN SELECT RAISE(ABORT, 'portfolio_snapshot is append-only'); END;
        CREATE TRIGGER portfolio_holding_no_update BEFORE UPDATE ON portfolio_holding BEGIN SELECT RAISE(ABORT, 'portfolio_holding is append-only'); END;
        CREATE TRIGGER portfolio_holding_no_delete BEFORE DELETE ON portfolio_holding BEGIN SELECT RAISE(ABORT, 'portfolio_holding is append-only'); END;
        CREATE TRIGGER portfolio_market_input_no_update BEFORE UPDATE ON portfolio_market_input BEGIN SELECT RAISE(ABORT, 'portfolio_market_input is append-only'); END;
        CREATE TRIGGER portfolio_market_input_no_delete BEFORE DELETE ON portfolio_market_input BEGIN SELECT RAISE(ABORT, 'portfolio_market_input is append-only'); END;
        CREATE TRIGGER portfolio_signal_input_no_update BEFORE UPDATE ON portfolio_signal_input BEGIN SELECT RAISE(ABORT, 'portfolio_signal_input is append-only'); END;
        CREATE TRIGGER portfolio_signal_input_no_delete BEFORE DELETE ON portfolio_signal_input BEGIN SELECT RAISE(ABORT, 'portfolio_signal_input is append-only'); END;
        CREATE TRIGGER thesis_break_event_no_update BEFORE UPDATE ON thesis_break_event BEGIN SELECT RAISE(ABORT, 'thesis_break_event is append-only'); END;
        CREATE TRIGGER thesis_break_event_no_delete BEFORE DELETE ON thesis_break_event BEGIN SELECT RAISE(ABORT, 'thesis_break_event is append-only'); END;
        CREATE TRIGGER thesis_break_verification_no_update BEFORE UPDATE ON thesis_break_verification BEGIN SELECT RAISE(ABORT, 'thesis_break_verification is append-only'); END;
        CREATE TRIGGER thesis_break_verification_no_delete BEFORE DELETE ON thesis_break_verification BEGIN SELECT RAISE(ABORT, 'thesis_break_verification is append-only'); END;
        CREATE TRIGGER portfolio_run_no_update BEFORE UPDATE ON portfolio_run BEGIN SELECT RAISE(ABORT, 'portfolio_run is append-only'); END;
        CREATE TRIGGER portfolio_run_no_delete BEFORE DELETE ON portfolio_run BEGIN SELECT RAISE(ABORT, 'portfolio_run is append-only'); END;
        CREATE TRIGGER portfolio_state_observation_no_update BEFORE UPDATE ON portfolio_state_observation BEGIN SELECT RAISE(ABORT, 'portfolio_state_observation is append-only'); END;
        CREATE TRIGGER portfolio_state_observation_no_delete BEFORE DELETE ON portfolio_state_observation BEGIN SELECT RAISE(ABORT, 'portfolio_state_observation is append-only'); END;
        CREATE TRIGGER portfolio_state_transition_no_update BEFORE UPDATE ON portfolio_state_transition BEGIN SELECT RAISE(ABORT, 'portfolio_state_transition is append-only'); END;
        CREATE TRIGGER portfolio_state_transition_no_delete BEFORE DELETE ON portfolio_state_transition BEGIN SELECT RAISE(ABORT, 'portfolio_state_transition is append-only'); END;
        CREATE TRIGGER portfolio_activity_day_no_update BEFORE UPDATE ON portfolio_activity_day BEGIN SELECT RAISE(ABORT, 'portfolio_activity_day is append-only'); END;
        CREATE TRIGGER portfolio_activity_day_no_delete BEFORE DELETE ON portfolio_activity_day BEGIN SELECT RAISE(ABORT, 'portfolio_activity_day is append-only'); END;
        CREATE TRIGGER trade_proposal_no_update BEFORE UPDATE ON trade_proposal BEGIN SELECT RAISE(ABORT, 'trade_proposal is append-only'); END;
        CREATE TRIGGER trade_proposal_no_delete BEFORE DELETE ON trade_proposal BEGIN SELECT RAISE(ABORT, 'trade_proposal is append-only'); END;
        CREATE TRIGGER portfolio_briefing_no_update BEFORE UPDATE ON portfolio_briefing BEGIN SELECT RAISE(ABORT, 'portfolio_briefing is append-only'); END;
        CREATE TRIGGER portfolio_briefing_no_delete BEFORE DELETE ON portfolio_briefing BEGIN SELECT RAISE(ABORT, 'portfolio_briefing is append-only'); END;
        """,
    ),
    (
        11,
        "V2 Phase 4 safe execution linkage",
        """
        CREATE TABLE phase4_execution_link (
            proposal_id TEXT PRIMARY KEY REFERENCES trade_proposal(proposal_id),
            execution_ref TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            approval_ref_hash TEXT CHECK(approval_ref_hash IS NULL OR length(approval_ref_hash)=64),
            broker_order_ref TEXT,
            previewed_at TEXT,
            approved_at TEXT,
            submitted_at TEXT,
            reconciled_at TEXT,
            settlement_ref TEXT,
            rendered_context_hash TEXT CHECK(rendered_context_hash IS NULL OR length(rendered_context_hash)=64)
        );
        CREATE INDEX phase4_execution_link_state_idx ON phase4_execution_link(state);
        """,
    ),
)

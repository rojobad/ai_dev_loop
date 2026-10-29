-- Phase 30.1: compact usage facts. No artifact walk and no inference.

CREATE TABLE scheduler_usage_attempt_facts (
    logical_attempt_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('cursor', 'codex')),
    role TEXT NOT NULL CHECK (role IN ('implementer', 'reviewer')),
    stage TEXT NOT NULL CHECK (stage IN ('initial', 'correction', 'review')),
    configured_model TEXT,
    terminal_status TEXT NOT NULL CHECK (
        terminal_status IN ('completed', 'failed', 'cancelled', 'uncertain')
    ),
    coverage TEXT CHECK (
        coverage IS NULL
        OR coverage IN ('complete', 'partial', 'unknown', 'not_applicable')
    ),
    settlement_status TEXT NOT NULL CHECK (
        settlement_status IN ('settled', 'pending', 'debt', 'conflict')
    ),
    diagnostic_codes TEXT NOT NULL,
    fact_payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    normalization_version TEXT NOT NULL,
    import_origin TEXT NOT NULL CHECK (import_origin IN ('ingestion', 'reconciliation')),
    source_paths TEXT NOT NULL,
    source_digests TEXT NOT NULL,
    publisher_attempt_id TEXT NOT NULL,
    source_dispatch_id TEXT NOT NULL,
    completion_fence_id TEXT,
    binding_kind TEXT NOT NULL CHECK (
        binding_kind IN ('completion_envelope', 'ledger_attempt')
    ),
    binding_sha256 TEXT NOT NULL CHECK (length(binding_sha256) = 64),
    acknowledgement TEXT NOT NULL CHECK (acknowledgement IN ('pending', 'acknowledged')),
    provenance_json TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE scheduler_usage_observations (
    observation_id TEXT NOT NULL PRIMARY KEY,
    logical_attempt_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (provider IN ('cursor', 'codex')),
    role TEXT NOT NULL CHECK (role IN ('implementer', 'reviewer')),
    stage TEXT NOT NULL CHECK (stage IN ('initial', 'correction', 'review')),
    native_key TEXT NOT NULL,
    publisher_attempt_id TEXT NOT NULL,
    source_dispatch_id TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
    source_format TEXT NOT NULL,
    source_event_ordinal INTEGER NOT NULL CHECK (source_event_ordinal >= 1),
    source_event_offsets TEXT NOT NULL,
    capture_complete INTEGER NOT NULL CHECK (capture_complete IN (0, 1)),
    availability TEXT NOT NULL CHECK (availability IN ('known', 'partial')),
    native_payload TEXT NOT NULL,
    normalized_payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    normalization_version TEXT NOT NULL,
    import_origin TEXT NOT NULL CHECK (import_origin IN ('ingestion', 'reconciliation')),
    completion_fence_id TEXT,
    binding_kind TEXT NOT NULL CHECK (
        binding_kind IN ('completion_envelope', 'ledger_attempt')
    ),
    binding_sha256 TEXT NOT NULL CHECK (length(binding_sha256) = 64),
    provenance_json TEXT NOT NULL,
    verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, role, logical_attempt_id, native_key)
);

CREATE INDEX idx_scheduler_usage_facts_provider_role
    ON scheduler_usage_attempt_facts(provider, role, stage);

CREATE INDEX idx_scheduler_usage_facts_model
    ON scheduler_usage_attempt_facts(provider, role, configured_model);

CREATE INDEX idx_scheduler_usage_facts_run
    ON scheduler_usage_attempt_facts(run_id);

CREATE INDEX idx_scheduler_usage_facts_ack
    ON scheduler_usage_attempt_facts(acknowledgement, settlement_status);

CREATE INDEX idx_scheduler_usage_observations_attempt
    ON scheduler_usage_observations(logical_attempt_id);

CREATE INDEX idx_scheduler_usage_observations_provider_role
    ON scheduler_usage_observations(provider, role, stage);

-- Phase 20.6.5: authenticated rollover aggregate.

CREATE TABLE scheduler_authenticated_rollovers (
    rollover_id TEXT PRIMARY KEY,
    state_kind TEXT NOT NULL,
    state_payload TEXT NOT NULL,
    state_payload_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_run_id TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    rollover_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_rollovers_source
    ON scheduler_authenticated_rollovers(source_run_id);

CREATE INDEX idx_scheduler_rollovers_run
    ON scheduler_authenticated_rollovers(rollover_run_id)
    WHERE rollover_run_id IS NOT NULL;

CREATE TABLE scheduler_authenticated_rollover_idempotency (
    source_run_id TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    rollover_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_run_id, definition_sha256)
);

CREATE TABLE scheduler_sequence_rollover_resolutions (
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    rollover_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    rollover_run_id TEXT NOT NULL,
    resolution_payload TEXT NOT NULL,
    resolution_payload_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (sequence_id, ordinal, rollover_id)
);

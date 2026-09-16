-- Phase 20.6: authenticated fresh-review recovery aggregate.

CREATE TABLE scheduler_fresh_review_recoveries (
    recovery_id TEXT PRIMARY KEY,
    state_kind TEXT NOT NULL,
    state_payload TEXT NOT NULL,
    state_payload_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    source_run_id TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    recovery_run_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_recoveries_source
    ON scheduler_fresh_review_recoveries(source_run_id);

CREATE INDEX idx_scheduler_recoveries_run
    ON scheduler_fresh_review_recoveries(recovery_run_id)
    WHERE recovery_run_id IS NOT NULL;

CREATE TABLE scheduler_fresh_review_recovery_idempotency (
    source_run_id TEXT NOT NULL,
    definition_sha256 TEXT NOT NULL,
    recovery_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_run_id, definition_sha256)
);

CREATE TABLE scheduler_sequence_recovery_resolutions (
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    recovery_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    recovery_run_id TEXT NOT NULL,
    resolution_payload TEXT NOT NULL,
    resolution_payload_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (sequence_id, ordinal, recovery_id)
);

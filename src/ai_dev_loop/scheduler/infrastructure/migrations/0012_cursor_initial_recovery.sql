-- Phase 23.2: unique initial standalone Cursor recovery publication.

CREATE TABLE scheduler_cursor_initial_recoveries (
    recovery_key TEXT NOT NULL PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    failed_attempt_id TEXT NOT NULL,
    successor_run_id TEXT NOT NULL UNIQUE,
    dispatch_id TEXT NOT NULL,
    parent_recovery_key TEXT,
    status TEXT NOT NULL,
    intent_payload TEXT NOT NULL,
    intent_payload_sha256 TEXT NOT NULL,
    record_artifact_path TEXT,
    record_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_run_id, failed_attempt_id)
);

CREATE INDEX idx_scheduler_cursor_initial_recoveries_successor
    ON scheduler_cursor_initial_recoveries(successor_run_id);

CREATE INDEX idx_scheduler_cursor_initial_recoveries_status
    ON scheduler_cursor_initial_recoveries(status, successor_run_id);

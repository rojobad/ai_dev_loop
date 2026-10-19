-- Phase 25.3: durable exact-agent Cursor SDK native-run reconciliation.

CREATE TABLE scheduler_cursor_sdk_reconciliations (
    recovery_key TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    source_attempt_id TEXT NOT NULL UNIQUE,
    source_dispatch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    intent_payload TEXT NOT NULL,
    intent_payload_sha256 TEXT NOT NULL,
    result_artifact_path TEXT,
    result_sha256 TEXT,
    abort_requested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_cursor_sdk_reconciliations_run
    ON scheduler_cursor_sdk_reconciliations(run_id, status);

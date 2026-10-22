-- Phase 25.8: one-use SDK resume no-progress continuation authority.

CREATE TABLE scheduler_cursor_sdk_no_progress (
    logical_turn_key TEXT NOT NULL PRIMARY KEY,
    consumed_count INTEGER NOT NULL CHECK (consumed_count IN (0, 1)),
    status TEXT NOT NULL CHECK (
        status IN ('reserved', 'publishing', 'published', 'held', 'cancelled')
    ),
    source_run_id TEXT NOT NULL,
    source_attempt_id TEXT NOT NULL,
    witness_path TEXT NOT NULL,
    witness_sha256 TEXT NOT NULL,
    authorized_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    successor_run_id TEXT,
    recovery_key TEXT,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_cursor_sdk_no_progress_run
    ON scheduler_cursor_sdk_no_progress(source_run_id, status);

CREATE INDEX idx_scheduler_cursor_sdk_no_progress_due
    ON scheduler_cursor_sdk_no_progress(status, due_at);

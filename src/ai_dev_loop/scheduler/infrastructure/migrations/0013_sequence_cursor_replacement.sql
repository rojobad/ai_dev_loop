-- Phase 23.4: durable Cursor sequence replacement intents.

CREATE TABLE scheduler_sequence_cursor_replacements (
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_run_id TEXT NOT NULL,
    source_generation INTEGER NOT NULL,
    recovery_key TEXT NOT NULL,
    successor_run_id TEXT NOT NULL,
    successor_generation INTEGER NOT NULL,
    intent_payload TEXT NOT NULL,
    intent_payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    adopted_at TEXT,
    cancelled_at TEXT,
    PRIMARY KEY (source_run_id, recovery_key),
    UNIQUE (successor_run_id),
    UNIQUE (sequence_id, ordinal, source_generation)
);

CREATE INDEX idx_scheduler_sequence_cursor_replacements_sequence
    ON scheduler_sequence_cursor_replacements(sequence_id, ordinal);

-- Phase 20.8: durable sequence execution replacement intents for review recovery.

CREATE TABLE scheduler_sequence_execution_replacements (
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
    published_at TEXT,
    PRIMARY KEY (source_run_id, recovery_key),
    UNIQUE (successor_run_id),
    UNIQUE (sequence_id, ordinal, source_generation, recovery_key)
);

CREATE INDEX idx_scheduler_sequence_execution_replacements_sequence
    ON scheduler_sequence_execution_replacements(sequence_id, ordinal);

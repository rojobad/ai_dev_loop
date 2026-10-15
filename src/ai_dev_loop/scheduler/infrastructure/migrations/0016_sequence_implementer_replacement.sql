CREATE TABLE scheduler_sequence_implementer_replacements (
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    source_run_id TEXT NOT NULL,
    source_generation INTEGER NOT NULL CHECK (source_generation >= 1),
    recovery_key TEXT PRIMARY KEY,
    successor_run_id TEXT NOT NULL UNIQUE,
    successor_generation INTEGER NOT NULL CHECK (successor_generation >= 2),
    provider TEXT NOT NULL CHECK (provider = 'codex'),
    intent_payload TEXT NOT NULL,
    intent_payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    adopted_at TEXT,
    cancelled_at TEXT,
    UNIQUE (source_run_id, recovery_key),
    UNIQUE (sequence_id, ordinal, source_generation),
    FOREIGN KEY (sequence_id) REFERENCES scheduler_sequences(sequence_id),
    FOREIGN KEY (source_run_id) REFERENCES scheduler_runs(run_id),
    FOREIGN KEY (successor_run_id) REFERENCES scheduler_runs(run_id),
    CHECK (NOT (adopted_at IS NOT NULL AND cancelled_at IS NOT NULL))
);

CREATE INDEX idx_scheduler_sequence_implementer_replacements_sequence
ON scheduler_sequence_implementer_replacements(sequence_id, ordinal, source_generation);

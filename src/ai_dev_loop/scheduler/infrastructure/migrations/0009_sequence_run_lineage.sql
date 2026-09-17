-- Phase 20.7: per-ordinal run-attempt lineage for materialized sequence phases.

CREATE TABLE scheduler_sequence_run_attempts (
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    source_run_id TEXT,
    attempt_kind TEXT NOT NULL,
    materialized_at TEXT NOT NULL,
    terminal_outcome TEXT,
    resolved_at TEXT,
    PRIMARY KEY (sequence_id, ordinal, generation),
    FOREIGN KEY (sequence_id) REFERENCES scheduler_sequences(sequence_id) ON DELETE RESTRICT,
    UNIQUE (run_id)
);

CREATE INDEX idx_scheduler_sequence_run_attempts_sequence
    ON scheduler_sequence_run_attempts(sequence_id, ordinal, generation);

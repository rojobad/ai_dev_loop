-- Phase 20.1: immutable prepared sequence definitions and ordered entries.

CREATE TABLE scheduler_sequences (
    sequence_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    state_kind TEXT NOT NULL,
    project_name TEXT NOT NULL,
    repository_root TEXT NOT NULL,
    worktree_key TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    prepared_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);

CREATE TABLE scheduler_sequence_entries (
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    phase_name TEXT NOT NULL,
    planned_run_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (sequence_id, ordinal),
    FOREIGN KEY (sequence_id) REFERENCES scheduler_sequences(sequence_id) ON DELETE RESTRICT,
    UNIQUE (sequence_id, phase_name)
);

CREATE INDEX idx_scheduler_sequences_worktree ON scheduler_sequences(worktree_key);
CREATE INDEX idx_scheduler_sequence_entries_sequence ON scheduler_sequence_entries(sequence_id);

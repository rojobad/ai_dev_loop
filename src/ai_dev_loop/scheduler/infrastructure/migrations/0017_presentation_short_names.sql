-- Phase 28.1: mutable presentation metadata outside frozen scheduler payloads.

CREATE TABLE scheduler_run_presentation (
    run_id TEXT PRIMARY KEY,
    short_name TEXT NOT NULL CHECK (
        length(short_name) BETWEEN 1 AND 35 AND short_name = trim(short_name)
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES scheduler_runs(run_id) ON DELETE RESTRICT
);

CREATE TABLE scheduler_sequence_presentation (
    sequence_id TEXT PRIMARY KEY,
    short_name TEXT NOT NULL CHECK (
        length(short_name) BETWEEN 1 AND 35 AND short_name = trim(short_name)
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (sequence_id) REFERENCES scheduler_sequences(sequence_id) ON DELETE RESTRICT
);

CREATE TABLE scheduler_planned_run_presentation (
    planned_run_id TEXT PRIMARY KEY,
    sequence_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    short_name TEXT NOT NULL CHECK (
        length(short_name) BETWEEN 1 AND 35 AND short_name = trim(short_name)
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (sequence_id, ordinal),
    FOREIGN KEY (planned_run_id) REFERENCES scheduler_sequence_entries(planned_run_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (sequence_id, ordinal) REFERENCES scheduler_sequence_entries(sequence_id, ordinal)
        ON DELETE RESTRICT
);

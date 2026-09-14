-- Phase 20.1.1: review retry generations and blocked-run recovery successors.

CREATE TABLE scheduler_review_retry_generations (
    run_id TEXT NOT NULL,
    failure_generation INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    PRIMARY KEY (run_id, failure_generation)
);

CREATE TABLE scheduler_review_recovery_successors (
    source_run_id TEXT NOT NULL,
    recovery_key TEXT NOT NULL,
    successor_run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_run_id, recovery_key)
);

CREATE INDEX idx_scheduler_review_recovery_source
    ON scheduler_review_recovery_successors(source_run_id);

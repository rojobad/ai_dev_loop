-- Phase 30.2: resumable usage-backfill jobs. Rows store ids, revisions, and
-- diagnostic codes only. They do not store prompts, patches, or source text.

CREATE TABLE scheduler_usage_backfill_jobs (
    job_id TEXT NOT NULL PRIMARY KEY,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('all', 'run', 'project')),
    scope_value TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'stopped')),
    batch_size INTEGER NOT NULL CHECK (batch_size >= 1),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    high_water INTEGER NOT NULL CHECK (high_water >= 0),
    snapshot_count INTEGER NOT NULL CHECK (snapshot_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE scheduler_usage_backfill_items (
    job_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    attempt_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'materialized',
            'retained',
            'conflict',
            'skipped_unsettled',
            'deferred'
        )
    ),
    diagnostic_codes TEXT NOT NULL,
    PRIMARY KEY (job_id, attempt_id),
    UNIQUE (job_id, ordinal)
);

CREATE INDEX idx_scheduler_usage_backfill_items_status
    ON scheduler_usage_backfill_items(job_id, status, ordinal);

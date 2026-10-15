-- Phase 27.5: unique standalone Codex implementer failure recovery publication.

CREATE TABLE scheduler_codex_implementer_recoveries (
    recovery_key TEXT NOT NULL PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    failed_attempt_id TEXT NOT NULL,
    successor_run_id TEXT NOT NULL UNIQUE,
    failed_dispatch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    intent_payload TEXT NOT NULL,
    intent_payload_sha256 TEXT NOT NULL,
    intent_artifact_path TEXT,
    intent_artifact_sha256 TEXT,
    record_artifact_path TEXT,
    record_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_run_id, failed_attempt_id)
);

CREATE INDEX idx_scheduler_codex_implementer_recoveries_successor
    ON scheduler_codex_implementer_recoveries(successor_run_id);

CREATE INDEX idx_scheduler_codex_implementer_recoveries_status
    ON scheduler_codex_implementer_recoveries(status, successor_run_id);

-- Central scheduler engine schema v14: independent Codex implementer attempts

ALTER TABLE scheduler_attempts RENAME TO scheduler_attempts_v13;

CREATE TABLE scheduler_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    dispatch_id TEXT NOT NULL REFERENCES scheduler_effects(dispatch_id),
    component TEXT NOT NULL CHECK (component IN ('cursor', 'codex', 'codex_implementer')),
    iteration INTEGER NOT NULL CHECK (iteration >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('launching', 'active', 'completed', 'failed', 'cancelled', 'uncertain')
    ),
    backend_identity TEXT,
    launch_intent_sha256 TEXT,
    stdout_artifact_path TEXT,
    stderr_artifact_path TEXT,
    completion_envelope_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    launch_nonce TEXT,
    unit_identity TEXT,
    exit_code INTEGER,
    termination_class TEXT,
    capacity_claim_id TEXT,
    capacity_tick_generation INTEGER,
    completion_fence_id TEXT,
    result_artifact_path TEXT,
    launch_requested_at TEXT,
    completed_at TEXT,
    ingested INTEGER NOT NULL DEFAULT 0
);

INSERT INTO scheduler_attempts(
    attempt_id, run_id, dispatch_id, component, iteration, status,
    backend_identity, launch_intent_sha256, stdout_artifact_path,
    stderr_artifact_path, completion_envelope_sha256, created_at, updated_at,
    launch_nonce, unit_identity, exit_code, termination_class, capacity_claim_id,
    capacity_tick_generation, completion_fence_id, result_artifact_path,
    launch_requested_at, completed_at, ingested
)
SELECT
    attempt_id, run_id, dispatch_id, component, iteration, status,
    backend_identity, launch_intent_sha256, stdout_artifact_path,
    stderr_artifact_path, completion_envelope_sha256, created_at, updated_at,
    launch_nonce, unit_identity, exit_code, termination_class, capacity_claim_id,
    capacity_tick_generation, completion_fence_id, result_artifact_path,
    launch_requested_at, completed_at, ingested
FROM scheduler_attempts_v13;

DROP TABLE scheduler_attempts_v13;

CREATE INDEX idx_scheduler_attempts_run
    ON scheduler_attempts(run_id, component, iteration);

CREATE INDEX idx_scheduler_attempts_unit_identity
    ON scheduler_attempts(unit_identity)
    WHERE unit_identity IS NOT NULL;

CREATE UNIQUE INDEX idx_scheduler_attempts_active_per_run
    ON scheduler_attempts(run_id)
    WHERE status IN ('launching', 'active', 'uncertain');

CREATE INDEX idx_scheduler_attempts_dispatch
    ON scheduler_attempts(dispatch_id);

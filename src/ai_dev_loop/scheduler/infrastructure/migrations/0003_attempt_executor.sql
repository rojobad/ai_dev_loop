-- Central scheduler engine schema v3: attempt executor lifecycle fields

ALTER TABLE scheduler_attempts ADD COLUMN launch_nonce TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN unit_identity TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN exit_code INTEGER;
ALTER TABLE scheduler_attempts ADD COLUMN termination_class TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN capacity_claim_id TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN capacity_tick_generation INTEGER;
ALTER TABLE scheduler_attempts ADD COLUMN completion_fence_id TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN result_artifact_path TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN launch_requested_at TEXT;
ALTER TABLE scheduler_attempts ADD COLUMN completed_at TEXT;

CREATE INDEX idx_scheduler_attempts_unit_identity
    ON scheduler_attempts(unit_identity)
    WHERE unit_identity IS NOT NULL;

CREATE UNIQUE INDEX idx_scheduler_attempts_active_per_run
    ON scheduler_attempts(run_id)
    WHERE status IN ('launching', 'active', 'uncertain');

CREATE INDEX idx_scheduler_attempts_dispatch
    ON scheduler_attempts(dispatch_id);

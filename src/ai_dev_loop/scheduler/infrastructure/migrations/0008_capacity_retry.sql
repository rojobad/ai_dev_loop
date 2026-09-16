-- Phase 20.5: durable idempotency for manual capacity-wait review authorization.

CREATE TABLE scheduler_capacity_retry_generations (
    run_id TEXT NOT NULL,
    capacity_wait_generation INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    PRIMARY KEY (run_id, capacity_wait_generation)
);

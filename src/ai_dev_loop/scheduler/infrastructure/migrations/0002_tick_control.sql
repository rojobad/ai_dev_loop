-- Central scheduler engine schema v2: tick control, capacity, admission claims

CREATE TABLE scheduler_capacity (
    capacity_name TEXT PRIMARY KEY CHECK (capacity_name = 'global_active_agent'),
    max_value INTEGER NOT NULL CHECK (max_value = 1),
    holder_run_id TEXT REFERENCES scheduler_runs(run_id),
    holder_claim_id TEXT,
    holder_tick_generation INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE scheduler_run_tick_claims (
    claim_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    tick_owner_id TEXT NOT NULL,
    tick_lease_generation INTEGER NOT NULL CHECK (tick_lease_generation >= 1),
    purpose TEXT NOT NULL CHECK (purpose = 'admission'),
    expected_run_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'stale')),
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_scheduler_run_tick_claims_active_admission
    ON scheduler_run_tick_claims(run_id)
    WHERE status = 'active' AND purpose = 'admission';

CREATE INDEX idx_scheduler_run_tick_claims_run
    ON scheduler_run_tick_claims(run_id, status);

CREATE INDEX idx_scheduler_runs_controller_repo
    ON scheduler_runs(state_kind, worktree_key);

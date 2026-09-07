-- Central scheduler engine schema v1

CREATE TABLE scheduler_schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE scheduler_runs (
    run_id TEXT PRIMARY KEY,
    state_kind TEXT NOT NULL,
    state_payload TEXT NOT NULL,
    state_payload_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    idempotency_key TEXT NOT NULL,
    worktree_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (idempotency_key)
);

CREATE INDEX idx_scheduler_runs_state_kind ON scheduler_runs(state_kind);
CREATE INDEX idx_scheduler_runs_worktree ON scheduler_runs(worktree_key);

CREATE TABLE scheduler_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_kind TEXT NOT NULL,
    event_payload TEXT NOT NULL,
    event_payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE INDEX idx_scheduler_events_run_seq ON scheduler_events(run_id, sequence);

CREATE TABLE scheduler_effects (
    dispatch_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES scheduler_events(event_id),
    effect_ordinal INTEGER NOT NULL CHECK (effect_ordinal >= 0),
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    effect_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    effect_kind TEXT NOT NULL,
    effect_payload TEXT NOT NULL,
    effect_payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'claimed',
            'succeeded',
            'retry_wait',
            'uncertain',
            'blocked',
            'cancelled',
            'superseded'
        )
    ),
    available_at TEXT NOT NULL,
    claimed_run_version INTEGER,
    claim_id TEXT,
    claim_owner_id TEXT,
    claim_lease_generation INTEGER,
    claimed_at TEXT,
    completed_at TEXT,
    last_error_kind TEXT,
    last_error_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_event_id, effect_ordinal)
);

CREATE INDEX idx_scheduler_effects_run_history
    ON scheduler_effects(run_id, created_at, dispatch_id);
CREATE INDEX idx_scheduler_effects_eligible
    ON scheduler_effects(status, available_at, run_id);
CREATE UNIQUE INDEX idx_scheduler_effects_one_live_per_run
    ON scheduler_effects(run_id)
    WHERE status IN ('pending', 'claimed');

CREATE TABLE scheduler_timers (
    timer_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES scheduler_events(event_id),
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    timer_kind TEXT NOT NULL CHECK (timer_kind = 'retry_due'),
    due_at TEXT NOT NULL,
    target_effect_id TEXT NOT NULL,
    expected_run_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled', 'superseded')),
    fired_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_timers_eligible
    ON scheduler_timers(status, due_at, run_id);
CREATE UNIQUE INDEX idx_scheduler_timers_one_pending_per_run
    ON scheduler_timers(run_id)
    WHERE status = 'pending';

CREATE TABLE scheduler_tick_leases (
    lease_name TEXT PRIMARY KEY CHECK (lease_name = 'global'),
    owner_id TEXT,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    status TEXT NOT NULL CHECK (status IN ('inactive', 'active', 'expired')),
    acquired_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE scheduler_claims (
    claim_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    dispatch_id TEXT NOT NULL REFERENCES scheduler_effects(dispatch_id),
    owner_id TEXT NOT NULL,
    lease_generation INTEGER NOT NULL CHECK (lease_generation >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'stale')),
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_claims_run ON scheduler_claims(run_id, status);

CREATE TABLE scheduler_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    dispatch_id TEXT NOT NULL REFERENCES scheduler_effects(dispatch_id),
    component TEXT NOT NULL CHECK (component IN ('cursor', 'codex')),
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
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_attempts_run ON scheduler_attempts(run_id, component, iteration);

CREATE TABLE scheduler_repository_reservations (
    worktree_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scheduler_runs(run_id),
    repository_root TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_reservations_run
    ON scheduler_repository_reservations(run_id, status);

-- PR review v2 durable engine schema v1

CREATE TABLE pr_review_schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE pr_review_runs (
    run_id TEXT PRIMARY KEY,
    state_kind TEXT NOT NULL,
    state_payload TEXT NOT NULL,
    state_payload_sha256 TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_pr_review_runs_state_kind ON pr_review_runs(state_kind);

CREATE TABLE pr_review_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pr_review_runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_kind TEXT NOT NULL,
    event_payload TEXT NOT NULL,
    event_payload_sha256 TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('accepted', 'rejected', 'stale')),
    expected_run_version INTEGER,
    observed_run_version INTEGER NOT NULL,
    resulting_run_version INTEGER,
    resulting_state_payload TEXT,
    resulting_state_payload_sha256 TEXT,
    rejection_code TEXT,
    safe_detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    CHECK (
        (
            disposition = 'accepted'
            AND resulting_run_version = observed_run_version + 1
            AND resulting_state_payload IS NOT NULL
            AND resulting_state_payload_sha256 IS NOT NULL
            AND rejection_code IS NULL
            AND safe_detail IS NULL
        )
        OR (
            disposition IN ('rejected', 'stale')
            AND resulting_run_version IS NULL
            AND resulting_state_payload IS NULL
            AND resulting_state_payload_sha256 IS NULL
        )
    )
);

CREATE INDEX idx_pr_review_events_run_seq ON pr_review_events(run_id, sequence);

CREATE TABLE pr_review_effects (
    dispatch_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES pr_review_events(event_id),
    effect_ordinal INTEGER NOT NULL CHECK (effect_ordinal >= 0),
    run_id TEXT NOT NULL REFERENCES pr_review_runs(run_id),
    effect_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    effect_kind TEXT NOT NULL,
    effect_payload TEXT NOT NULL,
    effect_payload_sha256 TEXT NOT NULL,
    classification TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= attempt),
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

CREATE INDEX idx_pr_review_effects_run_history
    ON pr_review_effects(run_id, created_at, dispatch_id);
CREATE INDEX idx_pr_review_effects_effect_history
    ON pr_review_effects(run_id, effect_id, created_at);
CREATE INDEX idx_pr_review_effects_eligible
    ON pr_review_effects(status, available_at, run_id);
CREATE INDEX idx_pr_review_effects_claims
    ON pr_review_effects(run_id, status, claim_id);

CREATE UNIQUE INDEX idx_pr_review_effects_one_live_per_run
    ON pr_review_effects(run_id)
    WHERE status IN ('pending', 'claimed');

CREATE TABLE pr_review_timers (
    timer_id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES pr_review_events(event_id),
    run_id TEXT NOT NULL REFERENCES pr_review_runs(run_id),
    timer_kind TEXT NOT NULL CHECK (timer_kind = 'retry_due'),
    due_at TEXT NOT NULL,
    target_effect_id TEXT NOT NULL,
    expected_run_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'fired', 'cancelled', 'superseded')),
    fired_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_pr_review_timers_eligible
    ON pr_review_timers(status, due_at, run_id);
CREATE UNIQUE INDEX idx_pr_review_timers_one_pending_per_run
    ON pr_review_timers(run_id)
    WHERE status = 'pending';

CREATE TABLE pr_review_worker_leases (
    run_id TEXT PRIMARY KEY REFERENCES pr_review_runs(run_id),
    owner_id TEXT,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    status TEXT NOT NULL CHECK (status IN ('inactive', 'active', 'expired', 'aborted')),
    acquired_at TEXT,
    heartbeat_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

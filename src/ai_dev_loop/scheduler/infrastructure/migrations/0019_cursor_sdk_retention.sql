-- Phase 25.7 C: explicit Cursor SDK conversation retention policy and disposal.

CREATE TABLE scheduler_cursor_sdk_retention_policy (
    policy_id TEXT PRIMARY KEY CHECK (policy_id = 'cursor-sdk-retention'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    automatic_cleanup_enabled INTEGER NOT NULL CHECK (automatic_cleanup_enabled IN (0, 1)),
    retention_hours INTEGER NOT NULL CHECK (retention_hours = 168),
    decision_source TEXT NOT NULL CHECK (decision_source = 'operator_2026-10-20'),
    decision_at TEXT NOT NULL CHECK (decision_at = '2026-10-20T00:00:00.000000Z'),
    updated_at TEXT NOT NULL
);

INSERT INTO scheduler_cursor_sdk_retention_policy(
    policy_id, schema_version, automatic_cleanup_enabled, retention_hours,
    decision_source, decision_at, updated_at
) VALUES (
    'cursor-sdk-retention', 1, 1, 168,
    'operator_2026-10-20', '2026-10-20T00:00:00.000000Z',
    '2026-10-20T00:00:00.000000Z'
);

CREATE TABLE scheduler_cursor_sdk_conversation_fences (
    conversation_key TEXT PRIMARY KEY,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    holder_id TEXT,
    purpose TEXT NOT NULL CHECK (purpose IN ('inactive', 'dispatch', 'disposal')),
    status TEXT NOT NULL CHECK (status IN ('inactive', 'active')),
    acquired_at TEXT,
    expires_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE scheduler_cursor_sdk_disposals (
    conversation_key TEXT PRIMARY KEY,
    conversation_owner_run_id TEXT NOT NULL,
    store_backend TEXT NOT NULL,
    native_store_layout_version INTEGER NOT NULL,
    intent_id TEXT NOT NULL UNIQUE,
    receipt_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('intent', 'deleted')),
    mode TEXT NOT NULL CHECK (mode IN ('manual', 'automatic')),
    definitively_finished_at TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    deleted_at TEXT,
    fence_generation INTEGER NOT NULL,
    intent_sha256 TEXT NOT NULL,
    receipt_sha256 TEXT,
    receipt_payload TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_cursor_sdk_disposals_status
    ON scheduler_cursor_sdk_disposals(status);

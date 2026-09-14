-- Phase 20.3 checkpoint reconciliation holds block abort cleanup until handoff completes.

CREATE TABLE scheduler_checkpoint_holds (
    run_id TEXT PRIMARY KEY NOT NULL,
    intent_sha256 TEXT NOT NULL,
    hold_reason TEXT NOT NULL,
    ref_may_have_advanced INTEGER NOT NULL DEFAULT 0 CHECK (ref_may_have_advanced IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_scheduler_checkpoint_holds_intent
ON scheduler_checkpoint_holds(intent_sha256);

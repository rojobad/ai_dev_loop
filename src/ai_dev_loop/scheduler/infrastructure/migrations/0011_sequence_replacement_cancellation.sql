-- Phase 20.8 correction: durable cancellation for outstanding sequence replacement intents.

ALTER TABLE scheduler_sequence_execution_replacements
    ADD COLUMN cancelled_at TEXT;

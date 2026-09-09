-- Central scheduler engine schema v4: cursor workflow ingestion tracking

ALTER TABLE scheduler_attempts ADD COLUMN ingested INTEGER NOT NULL DEFAULT 0;

"""Explicit local edits for scheduler-owned presentation metadata."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.presentation import ShortNameUpdateResult, require_short_name
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


def set_run_short_name(
    run_id: str,
    short_name: object,
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> ShortNameUpdateResult:
    cleaned = require_short_name(short_name, field_name="--short-name")
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    changed_at = now or utc_now()
    with store.begin_immediate() as conn:
        row = conn.execute("SELECT 1 FROM scheduler_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"scheduler run not found: {run_id}",
            )
        previous = store.run_short_name(conn, run_id)
        store.insert_run_short_name(
            conn,
            run_id=run_id,
            short_name=cleaned,
            now=changed_at,
        )
    return ShortNameUpdateResult(
        owner_id=run_id,
        short_name=cleaned,
        previous_short_name=previous,
        changed=previous != cleaned,
    )


def set_sequence_short_name(
    sequence_id: str,
    short_name: object,
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> ShortNameUpdateResult:
    cleaned = require_short_name(short_name, field_name="--short-name")
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    changed_at = now or utc_now()
    with store.begin_immediate() as conn:
        row = conn.execute(
            "SELECT 1 FROM scheduler_sequences WHERE sequence_id = ?", (sequence_id,)
        ).fetchone()
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"scheduler sequence not found: {sequence_id}",
            )
        previous = store.sequence_short_name(conn, sequence_id)
        store.insert_sequence_short_name(
            conn,
            sequence_id=sequence_id,
            short_name=cleaned,
            now=changed_at,
        )
    return ShortNameUpdateResult(
        owner_id=sequence_id,
        short_name=cleaned,
        previous_short_name=previous,
        changed=previous != cleaned,
    )

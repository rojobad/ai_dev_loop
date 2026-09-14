"""Sequence ownership rules for repository reservations."""

from __future__ import annotations

import sqlite3

from ai_dev_loop.scheduler.domain.sequence import (
    ABORT_PENDING_SEQUENCE_STATE_KIND,
    ACTIVE_SEQUENCE_STATE_KIND,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def sequence_governs_run_reservation(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    """Return True when an active or abort-pending sequence still owns the run reservation."""
    row = conn.execute(
        """
        SELECT 1
        FROM scheduler_sequences
        WHERE state_kind IN (?, ?)
          AND json_extract(payload, '$.current_run_id') = ?
        LIMIT 1
        """,
        (ACTIVE_SEQUENCE_STATE_KIND, ABORT_PENDING_SEQUENCE_STATE_KIND, run_id),
    ).fetchone()
    if row is None:
        return False
    if store.has_unresolved_abort_hold(conn, run_id):
        return True
    if store.has_checkpoint_reconciliation_hold(conn, run_id):
        return True
    return True

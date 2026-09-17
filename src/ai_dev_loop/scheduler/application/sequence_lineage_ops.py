"""Write-once sequence run-attempt lineage operations."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal, cast

from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_PLANNED,
    MaterializedSequenceState,
    SequenceAttemptKind,
    SequenceRunAttempt,
    SequenceTerminalOutcome,
)
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    persist_authoritative_lineage_from_state,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def insert_planned_run_attempt(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
    run_id: str,
    planned_run_id: str,
    materialized_at: str,
) -> None:
    if not store.schema_supports_sequence_run_lineage(conn):
        return
    attempt = SequenceRunAttempt(
        schema_version=1,
        generation=1,
        run_id=run_id,
        source_run_id=None,
        attempt_kind=cast(SequenceAttemptKind, SEQUENCE_ATTEMPT_KIND_PLANNED),
        materialized_at=materialized_at,
        terminal_outcome=None,
        resolved_at=None,
    )
    store.insert_sequence_run_attempt(
        conn,
        sequence_id=sequence_id,
        ordinal=ordinal,
        attempt=attempt,
        planned_run_id=planned_run_id,
    )


def resolve_sequence_run_terminal(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
    run_id: str,
    terminal_outcome: SequenceTerminalOutcome,
    resolved_at: datetime,
) -> None:
    if not store.schema_supports_sequence_run_lineage(conn):
        return
    store.resolve_sequence_attempt_terminal(
        conn,
        sequence_id=sequence_id,
        ordinal=ordinal,
        run_id=run_id,
        terminal_outcome=terminal_outcome,
        resolved_at=resolved_at,
    )


def sync_authoritative_lineage_from_state(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: MaterializedSequenceState,
) -> None:
    if not store.schema_supports_sequence_run_lineage(conn):
        return
    persist_authoritative_lineage_from_state(conn, state)


def resolve_accepted_phase_handoff(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    predecessor_ordinal: int,
    predecessor_run_id: str,
    accepted_outcome: Literal["completed", "completed_with_residual_risk"],
    successor_ordinal: int,
    successor_run_id: str,
    successor_planned_run_id: str,
    successor_materialized_at: str,
    resolved_at: datetime,
) -> None:
    if not store.schema_supports_sequence_run_lineage(conn):
        return
    resolve_sequence_run_terminal(
        store,
        conn,
        sequence_id=sequence_id,
        ordinal=predecessor_ordinal,
        run_id=predecessor_run_id,
        terminal_outcome=accepted_outcome,
        resolved_at=resolved_at,
    )
    insert_planned_run_attempt(
        store,
        conn,
        sequence_id=sequence_id,
        ordinal=successor_ordinal,
        run_id=successor_run_id,
        planned_run_id=successor_planned_run_id,
        materialized_at=successor_materialized_at,
    )

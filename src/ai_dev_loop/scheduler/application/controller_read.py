"""Central-ledger read adapter for controller status."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    ControllerSchedulerCandidate,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    scheduler_status_projection_from_state,
    summary_from_context,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _last_event_kind(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT event_kind FROM scheduler_events
        WHERE run_id = ?
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _candidate_from_validated_row(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    holder_run_id: str | None,
) -> ControllerSchedulerCandidate:
    db_run_id = str(row["run_id"])
    state, _, _ = store.load_validated_snapshot(conn, db_run_id)
    projection = scheduler_status_projection_from_state(state)
    summary = summary_from_context(
        run_id=state.run_id,
        state_kind=state.kind,
        submitted_at=state.submitted_at,
        updated_at=state.updated_at,
        context=state.context,
        safe_next_action=safe_next_action_for_scheduler_state(store, conn, state),
        cursor_wait_until=projection["cursor_wait_until"],
        block_reason_kind=projection["block_reason_kind"],
    )
    return ControllerSchedulerCandidate(
        run_id=state.run_id,
        state_kind=state.kind,
        project_name=summary.project_name,
        repository_root=summary.repository_root,
        submitted_at=state.submitted_at,
        updated_at=state.updated_at,
        safe_next_action=summary.safe_next_action,
        capacity_holder_run_id=holder_run_id,
        last_event_kind=_last_event_kind(conn, db_run_id),
        cursor_wait_until=projection["cursor_wait_until"],
        block_reason_kind=projection["block_reason_kind"],
    )


def find_scheduler_candidates(
    *,
    controller_session_id: str,
    repository_root: Path,
    include_terminal: bool = False,
    db_path: Path | None = None,
) -> list[ControllerSchedulerCandidate]:
    path = db_path or default_engine_db_path()
    if not path.exists():
        return []
    store = SqliteSchedulerStore.open_readonly(path)
    repo_text = str(repository_root.resolve())
    with store.begin_read() as conn:
        rows = store.find_runs_for_controller(
            conn,
            controller_session_id=controller_session_id,
            repository_root=repo_text,
            include_terminal=include_terminal,
        )
        capacity = store.get_capacity_row(conn)
        holder_run_id = (
            str(capacity["holder_run_id"]) if capacity["holder_run_id"] is not None else None
        )
        return [
            _candidate_from_validated_row(
                store,
                conn,
                row,
                holder_run_id=holder_run_id,
            )
            for row in rows
        ]


def load_scheduler_candidate(
    *,
    run_id: str,
    controller_session_id: str,
    repository_root: Path,
    db_path: Path | None = None,
) -> ControllerSchedulerCandidate:
    path = db_path or default_engine_db_path()
    if not path.exists():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    store = SqliteSchedulerStore.open_readonly(path)
    repo_text = str(repository_root.resolve())
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.context.controller.controller_session_id != controller_session_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "controller session id does not match the selected run",
            )
        if str(state.context.repository.root) != repo_text:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "repository path does not match the selected run repository root",
            )
        capacity = store.get_capacity_row(conn)
        holder_run_id = (
            str(capacity["holder_run_id"]) if capacity["holder_run_id"] is not None else None
        )
        projection = scheduler_status_projection_from_state(state)
        summary = summary_from_context(
            run_id=state.run_id,
            state_kind=state.kind,
            submitted_at=state.submitted_at,
            updated_at=state.updated_at,
            context=state.context,
            safe_next_action=safe_next_action_for_scheduler_state(store, conn, state),
            cursor_wait_until=projection["cursor_wait_until"],
            block_reason_kind=projection["block_reason_kind"],
        )
        return ControllerSchedulerCandidate(
            run_id=state.run_id,
            state_kind=state.kind,
            project_name=summary.project_name,
            repository_root=summary.repository_root,
            submitted_at=state.submitted_at,
            updated_at=state.updated_at,
            safe_next_action=summary.safe_next_action,
            capacity_holder_run_id=holder_run_id,
            last_event_kind=_last_event_kind(conn, state.run_id),
            cursor_wait_until=projection["cursor_wait_until"],
            block_reason_kind=projection["block_reason_kind"],
        )

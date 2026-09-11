"""Read-only scheduler status and list projections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SchedulerRunSummary,
    SchedulerStatusResult,
    bound_reviewer_session_id_from_state,
    scheduler_status_projection_from_state,
    summary_from_context,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.domain.state import SchedulerState
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SchedulerStatusService:
    def __init__(self, store: SqliteSchedulerStore) -> None:
        self.store = store

    def _summary_for_state(
        self,
        conn: sqlite3.Connection,
        state: SchedulerState,
    ) -> SchedulerRunSummary:
        projection = scheduler_status_projection_from_state(state)
        return summary_from_context(
            run_id=state.run_id,
            state_kind=state.kind,
            submitted_at=state.submitted_at,
            updated_at=state.updated_at,
            context=state.context,
            safe_next_action=safe_next_action_for_scheduler_state(self.store, conn, state),
            bound_reviewer_session_id=bound_reviewer_session_id_from_state(state),
            cursor_wait_until=projection["cursor_wait_until"],
            block_reason_kind=projection["block_reason_kind"],
        )

    def get_status(self, run_id: str) -> SchedulerStatusResult:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            summary = self._summary_for_state(conn, state)
            capacity = self.store.get_capacity_row(conn)
            last_event = conn.execute(
                """
                SELECT event_kind FROM scheduler_events
                WHERE run_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            return SchedulerStatusResult(
                summary=summary,
                idempotency_key_prefix=state.idempotency_key[:16],
                worktree_key_prefix=state.context.repository.worktree_key[:16],
                capacity_holder_run_id=(
                    str(capacity["holder_run_id"])
                    if capacity["holder_run_id"] is not None
                    else None
                ),
                last_event_kind=str(last_event[0]) if last_event is not None else None,
            )

    def list_runs(self) -> list[SchedulerRunSummary]:
        summaries: list[SchedulerRunSummary] = []
        with self.store.begin_read() as conn:
            for row in self.store.list_runs(conn):
                state, _, _ = self.store.load_validated_snapshot(conn, str(row["run_id"]))
                summaries.append(self._summary_for_state(conn, state))
        return summaries


def default_status_service(*, db_path: Path | None = None) -> SchedulerStatusService:
    path = db_path or default_engine_db_path()
    return SchedulerStatusService(SqliteSchedulerStore.open_readonly(path))


def scheduler_status(run_id: str, *, db_path: Path | None = None) -> SchedulerStatusResult:
    path = db_path or default_engine_db_path()
    if not path.exists():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    return default_status_service(db_path=path).get_status(run_id)


def scheduler_list(*, db_path: Path | None = None) -> list[SchedulerRunSummary]:
    path = db_path or default_engine_db_path()
    if not path.exists():
        return []
    return default_status_service(db_path=path).list_runs()

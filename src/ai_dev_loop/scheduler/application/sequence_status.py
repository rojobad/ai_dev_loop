"""Scheduler sequence status service."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SequenceEntrySummary,
    SequenceStatusResult,
    prepared_sequence_safe_next_action,
)
from ai_dev_loop.scheduler.domain.sequence import (
    PREPARED_SEQUENCE_STATE_KIND,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SequenceStatusService:
    def __init__(self, store: SqliteSchedulerStore) -> None:
        self.store = store

    def get_status(self, sequence_id: str) -> SequenceStatusResult:
        with self.store.begin_read() as conn:
            state = self._load_sequence(conn, sequence_id)
            return self._result_from_state(state)

    def _load_sequence(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
    ) -> PreparedSequenceState:
        self.store.require_sequence_schema(conn)
        try:
            return self.store.load_validated_sequence_state(conn, sequence_id)
        except SchedulerEngineError as exc:
            if exc.kind is SchedulerEngineErrorKind.NOT_FOUND:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.NOT_FOUND,
                    f"prepared sequence not found: {sequence_id}",
                ) from exc
            raise

    @staticmethod
    def _result_from_state(state: PreparedSequenceState) -> SequenceStatusResult:
        definition = state.definition
        entries = tuple(
            SequenceEntrySummary(
                ordinal=entry.ordinal,
                phase_name=entry.phase_name,
                planned_run_id_prefix=entry.planned_run_id[:8],
                commit_message_present=entry.commit_message is not None,
            )
            for entry in definition.entries
        )
        return SequenceStatusResult(
            sequence_id=state.sequence_id,
            name=definition.name,
            state_kind=PREPARED_SEQUENCE_STATE_KIND,
            project_name=definition.project_name,
            repository_root=definition.repository.root,
            entry_count=len(definition.entries),
            current_ordinal=None,
            prepared_at=state.prepared_at,
            updated_at=state.updated_at,
            idempotency_key_prefix=state.idempotency_key[:16],
            entries=entries,
            safe_next_action=prepared_sequence_safe_next_action(),
        )


def default_sequence_status_service(*, db_path: Path | None = None) -> SequenceStatusService:
    store = SqliteSchedulerStore.open_readonly(db_path or default_engine_db_path())
    return SequenceStatusService(store)


def scheduler_sequence_status(sequence_id: str) -> SequenceStatusResult:
    return default_sequence_status_service().get_status(sequence_id)

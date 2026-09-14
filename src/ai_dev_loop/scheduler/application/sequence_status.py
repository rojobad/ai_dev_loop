"""Scheduler sequence status service."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SequenceEntrySummary,
    SequenceStatusResult,
    active_sequence_safe_next_action,
    awaiting_finalization_sequence_safe_next_action,
    prepared_sequence_safe_next_action,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.domain.sequence import (
    ACTIVE_SEQUENCE_STATE_KIND,
    AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    PREPARED_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_TERMINAL_STATE_KINDS,
    CheckpointPendingState,
    CompletedWithResidualRiskState,
    SchedulerState,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SequenceStatusService:
    def __init__(self, store: SqliteSchedulerStore) -> None:
        self.store = store

    def get_status(self, sequence_id: str) -> SequenceStatusResult:
        with self.store.begin_read() as conn:
            state = self._load_sequence(conn, sequence_id)
            return self._result_from_state(conn, state)

    def _load_sequence(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
    ) -> PreparedSequenceState | ActiveSequenceState | AwaitingFinalizationSequenceState:
        self.store.require_sequence_schema(conn)
        try:
            loaded = self.store.load_validated_sequence_state(conn, sequence_id)
        except SchedulerEngineError as exc:
            if exc.kind is SchedulerEngineErrorKind.NOT_FOUND:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.NOT_FOUND,
                    f"prepared sequence not found: {sequence_id}",
                ) from exc
            raise
        if isinstance(
            loaded,
            (PreparedSequenceState, ActiveSequenceState, AwaitingFinalizationSequenceState),
        ):
            return loaded
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence state has unexpected type",
        )

    def _result_from_state(
        self,
        conn: sqlite3.Connection,
        state: PreparedSequenceState | ActiveSequenceState | AwaitingFinalizationSequenceState,
    ) -> SequenceStatusResult:
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
        if isinstance(state, AwaitingFinalizationSequenceState):
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                state_kind=AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                current_ordinal=len(definition.entries),
                current_run_id=state.final_run_id,
                current_run_state_kind=state.final_outcome,
                current_phase_name=definition.entries[-1].phase_name,
                residual_risk=bool(state.residual_risk_ordinals),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                started_at=state.started_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                safe_next_action=awaiting_finalization_sequence_safe_next_action(state.sequence_id),
            )

        if isinstance(state, PreparedSequenceState):
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
                safe_next_action=prepared_sequence_safe_next_action(state.sequence_id),
            )

        run_state, _, _ = self.store.load_validated_snapshot(conn, state.current_run_id)
        current_phase = definition.entries[state.current_ordinal - 1].phase_name
        residual_risk = bool(state.residual_risk_ordinals) or isinstance(
            run_state, CompletedWithResidualRiskState
        )
        safe_action = self._safe_action_for_active(conn, state, run_state)
        return SequenceStatusResult(
            sequence_id=state.sequence_id,
            name=definition.name,
            state_kind=ACTIVE_SEQUENCE_STATE_KIND,
            project_name=definition.project_name,
            repository_root=definition.repository.root,
            entry_count=len(definition.entries),
            current_ordinal=state.current_ordinal,
            current_run_id=state.current_run_id,
            current_run_state_kind=run_state.kind,
            current_phase_name=current_phase,
            residual_risk=residual_risk
            if run_state.kind in SCHEDULER_TERMINAL_STATE_KINDS
            else None,
            prepared_at=state.prepared_at,
            updated_at=state.updated_at,
            started_at=state.started_at,
            idempotency_key_prefix=state.idempotency_key[:16],
            entries=entries,
            safe_next_action=safe_action,
        )

    def _safe_action_for_active(
        self,
        conn: sqlite3.Connection,
        sequence_state: ActiveSequenceState,
        run_state: SchedulerState,
    ) -> SafeNextAction:
        run_action = safe_next_action_for_scheduler_state(self.store, conn, run_state)
        if isinstance(run_state, CheckpointPendingState):
            return active_sequence_safe_next_action(
                sequence_id=sequence_state.sequence_id,
                run_safe_action=run_action,
            )
        return active_sequence_safe_next_action(
            sequence_id=sequence_state.sequence_id,
            run_safe_action=run_action,
        )


def default_sequence_status_service(*, db_path: Path | None = None) -> SequenceStatusService:
    store = SqliteSchedulerStore.open_readonly(db_path or default_engine_db_path())
    return SequenceStatusService(store)


def scheduler_sequence_status(sequence_id: str) -> SequenceStatusResult:
    return default_sequence_status_service().get_status(sequence_id)

"""Scheduler sequence status service."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SequenceAggregateCounts,
    SequenceEntrySummary,
    SequenceStatusResult,
    abort_pending_sequence_safe_next_action,
    aborted_sequence_safe_next_action,
    active_sequence_safe_next_action,
    awaiting_finalization_sequence_safe_next_action,
    blocked_sequence_safe_next_action,
    prepared_sequence_safe_next_action,
    recovery_integrated_finalization_sequence_safe_next_action,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.application.sequence_report import SEQUENCE_COMPLETION_REPORT_ARTIFACT
from ai_dev_loop.scheduler.domain.checkpoint import SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
from ai_dev_loop.scheduler.domain.sequence import (
    ABORT_PENDING_SEQUENCE_STATE_KIND,
    ABORTED_SEQUENCE_STATE_KIND,
    ACTIVE_SEQUENCE_STATE_KIND,
    AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    BLOCKED_SEQUENCE_STATE_KIND,
    PREPARED_SEQUENCE_STATE_KIND,
    RECOVERY_INTEGRATED_FINALIZATION_SEQUENCE_STATE_KIND,
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    PreparedSequenceState,
    RecoveryIntegratedFinalizationSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_TERMINAL_STATE_KINDS,
    CompletedState,
    CompletedWithResidualRiskState,
    SchedulerState,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SequenceStatusService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        artifacts: ProtectedArtifactStore | None = None,
    ) -> None:
        self.store = store
        self._artifacts = artifacts

    def get_status(self, sequence_id: str) -> SequenceStatusResult:
        with self.store.begin_read() as conn:
            state = self._load_sequence(conn, sequence_id)
            return self._result_from_state(conn, state)

    def _load_sequence(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
    ) -> (
        PreparedSequenceState
        | ActiveSequenceState
        | AbortPendingSequenceState
        | BlockedSequenceState
        | AbortedSequenceState
        | AwaitingFinalizationSequenceState
        | RecoveryIntegratedFinalizationSequenceState
    ):
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
            (
                PreparedSequenceState,
                ActiveSequenceState,
                AbortPendingSequenceState,
                BlockedSequenceState,
                AbortedSequenceState,
                AwaitingFinalizationSequenceState,
                RecoveryIntegratedFinalizationSequenceState,
            ),
        ):
            return loaded
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence state has unexpected type",
        )

    def _entry_summaries(
        self,
        conn: sqlite3.Connection,
        state: (
            PreparedSequenceState
            | ActiveSequenceState
            | AbortPendingSequenceState
            | BlockedSequenceState
            | AbortedSequenceState
            | AwaitingFinalizationSequenceState
            | RecoveryIntegratedFinalizationSequenceState
        ),
    ) -> tuple[SequenceEntrySummary, ...]:
        definition = state.definition
        materialized_by_ordinal = {}
        if hasattr(state, "materialized_entries"):
            for entry in state.materialized_entries:
                materialized_by_ordinal[entry.ordinal] = entry
        cancelled: set[int] = set()
        if isinstance(state, (AbortPendingSequenceState, AbortedSequenceState)):
            cancelled = set(state.cancelled_ordinals)
        residual_ordinals: set[int] = set()
        if hasattr(state, "residual_risk_ordinals"):
            residual_ordinals = set(state.residual_risk_ordinals)
        summaries: list[SequenceEntrySummary] = []
        for entry in definition.entries:
            materialized = materialized_by_ordinal.get(entry.ordinal)
            accepted: str | None = None
            checkpoint_prefix: str | None = None
            resolution = self.store.get_sequence_recovery_resolution(
                conn,
                sequence_id=definition.sequence_id,
                ordinal=entry.ordinal,
            )
            if resolution is not None:
                from ai_dev_loop.scheduler.domain.recovery import SequenceRecoveryResolution

                assert isinstance(resolution, SequenceRecoveryResolution)
                accepted = resolution.accepted_outcome
                checkpoint_prefix = resolution.commit_sha256[:12]
            rollover_resolution = self.store.get_sequence_rollover_resolution(
                conn,
                sequence_id=definition.sequence_id,
                ordinal=entry.ordinal,
            )
            if rollover_resolution is not None:
                from ai_dev_loop.scheduler.domain.rollover import SequenceRolloverResolution

                assert isinstance(rollover_resolution, SequenceRolloverResolution)
                accepted = rollover_resolution.accepted_outcome
                checkpoint_prefix = rollover_resolution.commit_sha256[:12]
            if materialized is not None and accepted is None:
                run_state, _, _ = self.store.load_validated_snapshot(conn, materialized.run_id)
                if isinstance(run_state, CompletedState):
                    accepted = "completed"
                elif isinstance(run_state, CompletedWithResidualRiskState):
                    accepted = "completed_with_residual_risk"
                if self._artifacts is not None and entry.commit_message is not None:
                    result_path = (
                        self._artifacts.run_root(materialized.run_id)
                        / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
                    )
                    if result_path.is_file():
                        try:
                            result = json.loads(result_path.read_bytes())
                            commit_sha = str(result.get("commit_sha256", ""))
                            if commit_sha:
                                checkpoint_prefix = commit_sha[:12]
                        except (json.JSONDecodeError, OSError):
                            checkpoint_prefix = None
            summaries.append(
                SequenceEntrySummary(
                    ordinal=entry.ordinal,
                    phase_name=entry.phase_name,
                    planned_run_id_prefix=entry.planned_run_id[:8],
                    commit_message_present=entry.commit_message is not None,
                    materialized=materialized is not None,
                    materialized_run_id_prefix=(
                        materialized.run_id[:8] if materialized is not None else None
                    ),
                    accepted_outcome=accepted,
                    residual_risk=entry.ordinal in residual_ordinals,
                    checkpoint_commit_sha256_prefix=checkpoint_prefix,
                    cancelled=entry.ordinal in cancelled,
                )
            )
        return tuple(summaries)

    def _aggregate_counts(
        self,
        entries: tuple[SequenceEntrySummary, ...],
        entry_count: int,
    ) -> SequenceAggregateCounts:
        materialized = sum(1 for entry in entries if entry.materialized)
        accepted = sum(1 for entry in entries if entry.accepted_outcome is not None)
        residual_risk = sum(1 for entry in entries if entry.residual_risk)
        checkpointed = sum(
            1 for entry in entries if entry.checkpoint_commit_sha256_prefix is not None
        )
        cancelled = sum(1 for entry in entries if entry.cancelled)
        remaining = entry_count - materialized - cancelled
        return SequenceAggregateCounts(
            planned=entry_count,
            materialized=materialized,
            accepted=accepted,
            residual_risk=residual_risk,
            checkpointed=checkpointed,
            cancelled=cancelled,
            remaining=remaining,
        )

    def _completion_report_prefix(self, sequence_id: str) -> str | None:
        if self._artifacts is None:
            return None
        path = self._artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
        if not path.is_file():
            return None
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    def _result_from_state(
        self,
        conn: sqlite3.Connection,
        state: (
            PreparedSequenceState
            | ActiveSequenceState
            | AbortPendingSequenceState
            | BlockedSequenceState
            | AbortedSequenceState
            | AwaitingFinalizationSequenceState
            | RecoveryIntegratedFinalizationSequenceState
        ),
    ) -> SequenceStatusResult:
        definition = state.definition
        entries = self._entry_summaries(conn, state)
        counts = self._aggregate_counts(entries, len(definition.entries))

        if isinstance(state, RecoveryIntegratedFinalizationSequenceState):
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                aggregate_counts=counts,
                state_kind=RECOVERY_INTEGRATED_FINALIZATION_SEQUENCE_STATE_KIND,
                current_ordinal=len(definition.entries),
                current_run_id=state.recovery_run_id,
                current_run_state_kind=state.final_outcome,
                current_phase_name=definition.entries[-1].phase_name,
                residual_risk=bool(state.residual_risk_ordinals),
                residual_risk_ordinals=state.residual_risk_ordinals,
                finalized_at=state.finalized_at,
                started_at=state.started_at,
                completion_report_sha256_prefix=self._completion_report_prefix(state.sequence_id),
                safe_next_action=recovery_integrated_finalization_sequence_safe_next_action(
                    state.sequence_id
                ),
            )

        if isinstance(state, AwaitingFinalizationSequenceState):
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                aggregate_counts=counts,
                state_kind=AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
                current_ordinal=len(definition.entries),
                current_run_id=state.final_run_id,
                current_run_state_kind=state.final_outcome,
                current_phase_name=definition.entries[-1].phase_name,
                residual_risk=bool(state.residual_risk_ordinals),
                residual_risk_ordinals=state.residual_risk_ordinals,
                finalized_at=state.finalized_at,
                started_at=state.started_at,
                completion_report_sha256_prefix=self._completion_report_prefix(state.sequence_id),
                safe_next_action=awaiting_finalization_sequence_safe_next_action(state.sequence_id),
            )

        if isinstance(state, PreparedSequenceState):
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                aggregate_counts=counts,
                state_kind=PREPARED_SEQUENCE_STATE_KIND,
                current_ordinal=None,
                safe_next_action=prepared_sequence_safe_next_action(state.sequence_id),
            )

        if isinstance(state, AbortedSequenceState):
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                aggregate_counts=counts,
                state_kind=ABORTED_SEQUENCE_STATE_KIND,
                current_ordinal=state.current_ordinal,
                current_run_id=state.current_run_id,
                started_at=state.started_at,
                abort_reason=state.abort_reason,
                residual_risk=bool(state.residual_risk_ordinals),
                residual_risk_ordinals=state.residual_risk_ordinals,
                safe_next_action=aborted_sequence_safe_next_action(state.sequence_id),
            )

        if isinstance(state, BlockedSequenceState):
            run_state, _, _ = self.store.load_validated_snapshot(conn, state.current_run_id)
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                aggregate_counts=counts,
                state_kind=BLOCKED_SEQUENCE_STATE_KIND,
                current_ordinal=state.current_ordinal,
                current_run_id=state.current_run_id,
                current_run_state_kind=run_state.kind,
                current_phase_name=definition.entries[state.current_ordinal - 1].phase_name,
                residual_risk=bool(state.residual_risk_ordinals),
                residual_risk_ordinals=state.residual_risk_ordinals,
                block_reason_kind=state.block_reason_kind,
                started_at=state.started_at,
                safe_next_action=blocked_sequence_safe_next_action(
                    state.sequence_id,
                    block_reason_kind=state.block_reason_kind,
                ),
            )

        if isinstance(state, AbortPendingSequenceState):
            run_state, _, _ = self.store.load_validated_snapshot(conn, state.current_run_id)
            return SequenceStatusResult(
                sequence_id=state.sequence_id,
                name=definition.name,
                project_name=definition.project_name,
                repository_root=definition.repository.root,
                entry_count=len(definition.entries),
                prepared_at=state.prepared_at,
                updated_at=state.updated_at,
                idempotency_key_prefix=state.idempotency_key[:16],
                entries=entries,
                aggregate_counts=counts,
                state_kind=ABORT_PENDING_SEQUENCE_STATE_KIND,
                current_ordinal=state.current_ordinal,
                current_run_id=state.current_run_id,
                current_run_state_kind=run_state.kind,
                current_phase_name=definition.entries[state.current_ordinal - 1].phase_name,
                residual_risk=bool(state.residual_risk_ordinals),
                residual_risk_ordinals=state.residual_risk_ordinals,
                abort_reason=state.abort_reason,
                started_at=state.started_at,
                safe_next_action=abort_pending_sequence_safe_next_action(state.sequence_id),
            )

        assert isinstance(state, ActiveSequenceState)
        run_state, _, _ = self.store.load_validated_snapshot(conn, state.current_run_id)
        current_phase = definition.entries[state.current_ordinal - 1].phase_name
        residual_risk = bool(state.residual_risk_ordinals) or isinstance(
            run_state, CompletedWithResidualRiskState
        )
        safe_action = self._safe_action_for_active(conn, state, run_state)
        return SequenceStatusResult(
            sequence_id=state.sequence_id,
            name=definition.name,
            project_name=definition.project_name,
            repository_root=definition.repository.root,
            entry_count=len(definition.entries),
            prepared_at=state.prepared_at,
            updated_at=state.updated_at,
            idempotency_key_prefix=state.idempotency_key[:16],
            entries=entries,
            aggregate_counts=counts,
            state_kind=ACTIVE_SEQUENCE_STATE_KIND,
            current_ordinal=state.current_ordinal,
            current_run_id=state.current_run_id,
            current_run_state_kind=run_state.kind,
            current_phase_name=current_phase,
            residual_risk=residual_risk
            if run_state.kind in SCHEDULER_TERMINAL_STATE_KINDS
            else None,
            residual_risk_ordinals=state.residual_risk_ordinals,
            started_at=state.started_at,
            safe_next_action=safe_action,
        )

    def _safe_action_for_active(
        self,
        conn: sqlite3.Connection,
        sequence_state: ActiveSequenceState,
        run_state: SchedulerState,
    ) -> SafeNextAction:
        run_action = safe_next_action_for_scheduler_state(self.store, conn, run_state)
        return active_sequence_safe_next_action(
            sequence_id=sequence_state.sequence_id,
            run_safe_action=run_action,
        )


def default_sequence_status_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> SequenceStatusService:
    from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root

    store = SqliteSchedulerStore.open_readonly(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return SequenceStatusService(store, artifacts=artifacts)


def scheduler_sequence_status(sequence_id: str) -> SequenceStatusResult:
    return default_sequence_status_service().get_status(sequence_id)

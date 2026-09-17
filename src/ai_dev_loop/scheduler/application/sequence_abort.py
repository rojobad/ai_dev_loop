"""Sequence-level abort coordinator (durable-first, non-destructive)."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.abort import SchedulerAbortService, scheduler_abort_run
from ai_dev_loop.scheduler.application.attempt_backend import AgentProcessBackend
from ai_dev_loop.scheduler.application.contracts import (
    AbortProcessAction,
    SafeNextAction,
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SequenceAbortResult,
    aborted_sequence_safe_next_action,
)
from ai_dev_loop.scheduler.application.sequence_reconcile import SequenceReconcileService
from ai_dev_loop.scheduler.application.systemd_backend import SystemdUserBackend
from ai_dev_loop.scheduler.domain.events import SequenceAbortedEvent, SequenceAbortRequestedEvent
from ai_dev_loop.scheduler.domain.sequence import (
    ABORT_PENDING_SEQUENCE_STATE_KIND,
    ABORTED_SEQUENCE_STATE_KIND,
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    PreparedSequenceState,
    future_entry_ordinals,
)
from ai_dev_loop.scheduler.domain.state import SCHEDULER_TERMINAL_STATE_KINDS, AbortedState
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SequenceAbortService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        run_abort: SchedulerAbortService | None = None,
        reconcile: SequenceReconcileService | None = None,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self._run_abort = run_abort
        self._reconcile = reconcile or SequenceReconcileService(
            store,
            now_factory=now_factory,
            event_id_factory=event_id_factory,
        )
        self._now_factory = now_factory or (lambda: datetime.now(tz=UTC))
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")

    def abort_sequence(
        self,
        sequence_id: str,
        *,
        reason: str = "user_requested_abort",
    ) -> SequenceAbortResult:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            self.store.require_sequence_schema(conn)
            state = self.store.load_validated_sequence_state(conn, sequence_id)

        if isinstance(state, AbortedSequenceState):
            return self._idempotent_aborted(state)
        if isinstance(state, AwaitingFinalizationSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "cannot abort sequence awaiting finalization",
            )
        if isinstance(state, BlockedSequenceState):
            return self._abort_blocked(state, reason=reason, now=now)
        if isinstance(state, AbortPendingSequenceState):
            return self._reconcile_pending_abort(state, now=now)

        if isinstance(state, PreparedSequenceState):
            return self._abort_prepared(state, reason=reason, now=now)
        if isinstance(state, ActiveSequenceState):
            return self._abort_active(state, reason=reason, now=now)
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence state has unexpected type",
        )

    def continue_pending_abort_for_run(self, run_id: str) -> None:
        with self.store.begin_read() as conn:
            row = conn.execute(
                """
                SELECT sequence_id
                FROM scheduler_sequences
                WHERE state_kind = ?
                AND json_extract(payload, '$.current_run_id') = ?
                """,
                (ABORT_PENDING_SEQUENCE_STATE_KIND, run_id),
            ).fetchone()
            if row is None:
                return
            state = self.store.load_validated_sequence_state(conn, str(row["sequence_id"]))
        if not isinstance(state, AbortPendingSequenceState):
            return
        self._advance_pending_run_abort(state)

    def _idempotent_aborted(self, state: AbortedSequenceState) -> SequenceAbortResult:
        return SequenceAbortResult(
            sequence_id=state.sequence_id,
            state_kind=ABORTED_SEQUENCE_STATE_KIND,
            abort_persisted=True,
            idempotent_replay=True,
            run_abort_process_action=AbortProcessAction.NONE,
            run_termination_pending=False,
            safe_next_action=aborted_sequence_safe_next_action(state.sequence_id),
        )

    def _abort_prepared(
        self,
        state: PreparedSequenceState,
        *,
        reason: str,
        now: datetime,
    ) -> SequenceAbortResult:
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        total = len(state.definition.entries)
        cancelled = tuple(range(1, total + 1))
        aborted = AbortedSequenceState(
            schema_version=state.schema_version,
            sequence_id=state.sequence_id,
            version=state.version + 1,
            prepared_at=state.prepared_at,
            updated_at=now_text,
            started_at=None,
            aborted_at=now_text,
            abort_reason=reason,
            idempotency_key=state.idempotency_key,
            definition=state.definition,
            cancelled_ordinals=cancelled,
        )
        with self.store.begin_immediate() as conn:
            if not self.store.compare_and_swap_sequence_state(
                conn,
                sequence_id=state.sequence_id,
                expected_version=state.version,
                new_state=aborted,
                now=now,
            ):
                refreshed = self.store.load_validated_sequence_state(conn, state.sequence_id)
                if isinstance(refreshed, AbortedSequenceState):
                    return self._idempotent_aborted(refreshed)
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "sequence changed during prepared abort",
                )
        return SequenceAbortResult(
            sequence_id=state.sequence_id,
            state_kind=ABORTED_SEQUENCE_STATE_KIND,
            abort_persisted=True,
            idempotent_replay=False,
            run_abort_process_action=AbortProcessAction.NONE,
            run_termination_pending=False,
            safe_next_action=aborted_sequence_safe_next_action(state.sequence_id),
        )

    def _advance_pending_run_abort(self, state: AbortPendingSequenceState) -> AbortProcessAction:
        with self.store.begin_read() as conn:
            run_state, _, _ = self.store.load_validated_snapshot(conn, state.current_run_id)
        if isinstance(run_state, AbortedState):
            return AbortProcessAction.NONE
        if run_state.kind in SCHEDULER_TERMINAL_STATE_KINDS:
            return AbortProcessAction.NONE
        if self._run_abort is not None:
            result = self._run_abort.abort_run(
                state.current_run_id,
                reason=state.abort_reason,
            )
            return result.process_action
        scheduler_abort_run(
            state.current_run_id,
            reason=state.abort_reason,
            db_path=self.store.db_path,
        )
        return AbortProcessAction.NONE

    def _abort_blocked(
        self,
        state: BlockedSequenceState,
        *,
        reason: str,
        now: datetime,
    ) -> SequenceAbortResult:
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        source_run_id = state.current_run_id
        cancelled = future_entry_ordinals(state.definition, state.current_ordinal)
        aborted = AbortedSequenceState(
            schema_version=state.schema_version,
            sequence_id=state.sequence_id,
            version=state.version + 1,
            prepared_at=state.prepared_at,
            updated_at=now_text,
            started_at=state.started_at,
            aborted_at=now_text,
            abort_reason=reason,
            idempotency_key=state.idempotency_key,
            definition=state.definition,
            current_ordinal=state.current_ordinal,
            current_run_id=source_run_id,
            materialized_entries=state.materialized_entries,
            residual_risk_ordinals=state.residual_risk_ordinals,
            cancelled_ordinals=cancelled,
            preserved_current_leaf_terminal="blocked",
            preserved_current_leaf_resolved_at=state.blocked_at,
        )
        abort_request = SequenceAbortRequestedEvent(
            sequence_id=state.sequence_id,
            reason=reason,
            current_run_id=source_run_id,
        )
        aborted_event = SequenceAbortedEvent(
            sequence_id=state.sequence_id,
            reason=reason,
            current_run_id=source_run_id,
            cancelled_ordinal_count=len(cancelled),
        )
        idempotent = False
        with self.store.begin_immediate() as conn:
            self.store.cancel_outstanding_sequence_execution_replacements(
                conn,
                sequence_id=state.sequence_id,
                source_run_id=source_run_id,
                now=now,
            )
            if not self.store.has_sequence_abort_requested_for_run(
                conn,
                run_id=source_run_id,
                sequence_id=state.sequence_id,
            ):
                event_id = self._event_id_factory()
                sequence_num = self.store.next_event_sequence(conn, source_run_id)
                self.store.append_event(
                    conn,
                    event_id=event_id,
                    run_id=source_run_id,
                    sequence=sequence_num,
                    event=abort_request,
                    now=now,
                )
            if not self.store.compare_and_swap_sequence_state(
                conn,
                sequence_id=state.sequence_id,
                expected_version=state.version,
                new_state=aborted,
                now=now,
            ):
                refreshed = self.store.load_validated_sequence_state(
                    conn,
                    state.sequence_id,
                    validate_lineage=False,
                )
                if isinstance(refreshed, AbortedSequenceState):
                    return self._idempotent_aborted(refreshed)
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "sequence changed during blocked abort",
                )
            event_id = self._event_id_factory()
            sequence_num = self.store.next_event_sequence(conn, source_run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=source_run_id,
                sequence=sequence_num,
                event=aborted_event,
                now=now,
            )
            worktree_key = state.definition.repository.worktree_key
            reservation = self.store.get_reservation_for_run(conn, source_run_id)
            if reservation is not None and str(reservation["worktree_key"]) == worktree_key:
                self.store.release_reservation(conn, worktree_key=worktree_key, now=now)
        with self.store.begin_read() as conn:
            self.store.load_validated_sequence_state(conn, state.sequence_id)
        return SequenceAbortResult(
            sequence_id=state.sequence_id,
            state_kind=ABORTED_SEQUENCE_STATE_KIND,
            abort_persisted=True,
            idempotent_replay=idempotent,
            run_abort_process_action=AbortProcessAction.NONE,
            run_termination_pending=False,
            safe_next_action=aborted_sequence_safe_next_action(state.sequence_id),
        )

    def _abort_active(
        self,
        state: ActiveSequenceState,
        *,
        reason: str,
        now: datetime,
    ) -> SequenceAbortResult:
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cancelled = future_entry_ordinals(state.definition, state.current_ordinal)
        pending = AbortPendingSequenceState(
            schema_version=state.schema_version,
            sequence_id=state.sequence_id,
            version=state.version + 1,
            prepared_at=state.prepared_at,
            updated_at=now_text,
            started_at=state.started_at,
            abort_requested_at=now_text,
            abort_reason=reason,
            idempotency_key=state.idempotency_key,
            definition=state.definition,
            current_ordinal=state.current_ordinal,
            current_run_id=state.current_run_id,
            materialized_entries=state.materialized_entries,
            residual_risk_ordinals=state.residual_risk_ordinals,
            cancelled_ordinals=cancelled,
        )
        abort_request = SequenceAbortRequestedEvent(
            sequence_id=state.sequence_id,
            reason=reason,
            current_run_id=state.current_run_id,
        )
        refreshed_pending: AbortPendingSequenceState | None = None
        with self.store.begin_immediate() as conn:
            if not self.store.has_sequence_abort_requested_for_run(
                conn,
                run_id=state.current_run_id,
                sequence_id=state.sequence_id,
            ):
                event_id = self._event_id_factory()
                sequence_num = self.store.next_event_sequence(conn, state.current_run_id)
                self.store.append_event(
                    conn,
                    event_id=event_id,
                    run_id=state.current_run_id,
                    sequence=sequence_num,
                    event=abort_request,
                    now=now,
                )
            if not self.store.compare_and_swap_sequence_state(
                conn,
                sequence_id=state.sequence_id,
                expected_version=state.version,
                new_state=pending,
                now=now,
            ):
                refreshed = self.store.load_validated_sequence_state(conn, state.sequence_id)
                if isinstance(refreshed, AbortPendingSequenceState):
                    refreshed_pending = refreshed
                elif isinstance(refreshed, AbortedSequenceState):
                    return self._idempotent_aborted(refreshed)
                else:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "sequence changed during active abort",
                    )
        if refreshed_pending is not None:
            return self._reconcile_pending_abort(refreshed_pending, now=now)

        run_action = self._advance_pending_run_abort(pending)
        with self.store.begin_immediate() as conn:
            receipt = self._reconcile.reconcile_run(conn, pending.current_run_id)
        state_kind = ABORT_PENDING_SEQUENCE_STATE_KIND
        termination_pending = True
        if receipt is not None and receipt.action == "sequence_aborted":
            state_kind = ABORTED_SEQUENCE_STATE_KIND
            termination_pending = False
        elif receipt is not None and receipt.action == "sequence_abort_termination_pending":
            termination_pending = True
        safe_action = (
            aborted_sequence_safe_next_action(state.sequence_id)
            if state_kind == ABORTED_SEQUENCE_STATE_KIND
            else SafeNextAction(
                kind=SafeNextActionKind.SCHEDULER_TICK,
                command=(
                    f"Sequence {state.sequence_id} abort is pending on run "
                    f"{state.current_run_id}. Run ai_dev_loop scheduler tick to reconcile."
                ),
            )
        )
        return SequenceAbortResult(
            sequence_id=state.sequence_id,
            state_kind=state_kind,
            abort_persisted=True,
            idempotent_replay=False,
            run_abort_process_action=run_action,
            run_termination_pending=termination_pending,
            safe_next_action=safe_action,
        )

    def _reconcile_pending_abort(
        self,
        state: AbortPendingSequenceState,
        *,
        now: datetime,
    ) -> SequenceAbortResult:
        del now
        self._advance_pending_run_abort(state)
        with self.store.begin_immediate() as conn:
            receipt = self._reconcile.reconcile_run(conn, state.current_run_id)
        if receipt is not None and receipt.action == "sequence_aborted":
            return SequenceAbortResult(
                sequence_id=state.sequence_id,
                state_kind=ABORTED_SEQUENCE_STATE_KIND,
                abort_persisted=True,
                idempotent_replay=True,
                run_abort_process_action=AbortProcessAction.NONE,
                run_termination_pending=False,
                safe_next_action=aborted_sequence_safe_next_action(state.sequence_id),
            )
        termination_pending = receipt is None or receipt.action in {
            "sequence_abort_run_pending",
            "sequence_abort_termination_pending",
        }
        return SequenceAbortResult(
            sequence_id=state.sequence_id,
            state_kind=ABORT_PENDING_SEQUENCE_STATE_KIND,
            abort_persisted=True,
            idempotent_replay=True,
            run_abort_process_action=AbortProcessAction.NONE,
            run_termination_pending=termination_pending,
            safe_next_action=SafeNextAction(
                kind=SafeNextActionKind.SCHEDULER_TICK,
                command=(
                    f"Sequence {state.sequence_id} abort is pending on run "
                    f"{state.current_run_id}. Run ai_dev_loop scheduler tick to reconcile."
                ),
            ),
        )


def default_sequence_abort_service(
    *,
    db_path: Path | None = None,
    backend: AgentProcessBackend | None = None,
    artifact_root: Path | None = None,
) -> SequenceAbortService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    run_abort = SchedulerAbortService(
        store,
        backend or SystemdUserBackend(),
        artifacts=artifacts,
    )
    return SequenceAbortService(store, run_abort=run_abort)


def scheduler_sequence_abort(
    sequence_id: str,
    *,
    reason: str = "user_requested_abort",
    db_path: Path | None = None,
    backend: AgentProcessBackend | None = None,
    artifact_root: Path | None = None,
) -> SequenceAbortResult:
    path = db_path or default_engine_db_path()
    if not path.exists():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    return default_sequence_abort_service(
        db_path=path,
        backend=backend,
        artifact_root=artifact_root,
    ).abort_sequence(sequence_id, reason=reason)

"""Central scheduler abort service (durable-first, exact unit ownership)."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ai_dev_loop.scheduler.application.abort_reconcile import (
    finalize_aborted_run_cleanup,
    reconcile_run_abort_attempts,
)
from ai_dev_loop.scheduler.application.attempt_backend import AgentProcessBackend
from ai_dev_loop.scheduler.application.checkpoint_abort_coordination import (
    checkpoint_cas_uncertainty_active,
    should_defer_checkpoint_abort_transition,
)
from ai_dev_loop.scheduler.application.contracts import (
    AbortProcessAction,
    AbortResult,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    checkpoint_pending_safe_next_action,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.application.systemd_backend import SystemdUserBackend
from ai_dev_loop.scheduler.domain.events import AbortRequestedEvent, RunAbortedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_ABORTABLE_STATE_KINDS,
    SCHEDULER_TERMINAL_STATE_KINDS,
    AbortedState,
    AdmittedState,
    AuthorizedState,
    AwaitingCodexReviewState,
    CheckpointPendingState,
    CursorReadyState,
    PreflightCompleteState,
    SubmittedState,
    WaitingCodexReviewRetryState,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def complete_recorded_checkpoint_abort_if_ready(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now: datetime,
    event_id_factory: Callable[[], str],
    fence_id: str,
    reason: str = "user_requested_abort",
) -> bool:
    """Complete a recorded abort for checkpoint_pending once CAS uncertainty is cleared.

    Returns True when the run is aborted (including idempotent replay on AbortedState).
    Returns False when prerequisites are not met.
    """
    state, version, _ = store.load_validated_snapshot(conn, run_id)
    if isinstance(state, AbortedState):
        if not store.has_unresolved_abort_hold(conn, run_id):
            finalize_aborted_run_cleanup(store, conn, run_id=run_id, now=now)
        return True
    if not isinstance(state, CheckpointPendingState):
        return False
    if not store.has_abort_requested_for_run(conn, run_id):
        return False
    if checkpoint_cas_uncertainty_active(store, conn, run_id=run_id):
        return False

    prior_state_kind = state.kind
    aborted_event = RunAbortedEvent(
        run_id=run_id,
        reason=reason,
        prior_state_kind=prior_state_kind,
    )
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    new_state = apply_run_aborted(state, aborted_event, now_text=now_text)
    aborted_event_id = event_id_factory()
    aborted_sequence = store.next_event_sequence(conn, run_id)
    store.append_event(
        conn,
        event_id=aborted_event_id,
        run_id=run_id,
        sequence=aborted_sequence,
        event=aborted_event,
        now=now,
    )
    if not store.compare_and_swap_state(
        conn,
        run_id=run_id,
        expected_version=version,
        new_state=new_state,
        now=now,
    ):
        refreshed, _, _ = store.load_validated_snapshot(conn, run_id)
        return isinstance(refreshed, AbortedState)
    hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
    if hold is not None and not int(hold["ref_may_have_advanced"]):
        store.release_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256=str(hold["intent_sha256"]),
        )
    store.cancel_live_work(conn, run_id=run_id, now=now)
    store.cancel_nonterminal_attempts(
        conn,
        run_id=run_id,
        completion_fence_id=fence_id,
        now=now,
    )
    if not store.has_unresolved_abort_hold(conn, run_id):
        finalize_aborted_run_cleanup(store, conn, run_id=run_id, now=now)
    return True


class SchedulerAbortService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        backend: AgentProcessBackend,
        *,
        artifacts: ProtectedArtifactStore | None = None,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        fence_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self._artifacts = artifacts
        self._now_factory = now_factory or (lambda: datetime.now(tz=UTC))
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._fence_id_factory = fence_id_factory or (lambda: f"fnc-{secrets.token_hex(16)}")

    def abort_run(
        self,
        run_id: str,
        *,
        reason: str = "user_requested_abort",
    ) -> AbortResult:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        if isinstance(state, AbortedState):
            return self._reconcile_existing_abort(run_id, now=now)
        if state.kind in SCHEDULER_TERMINAL_STATE_KINDS:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                f"cannot abort terminal scheduler run in state {state.kind}",
            )
        if state.kind not in SCHEDULER_ABORTABLE_STATE_KINDS:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                f"scheduler run state {state.kind} is not abortable",
            )

        prior_state_kind = state.kind
        fence_id = self._fence_id_factory()
        concurrent_aborted = False
        deferred_checkpoint_abort = False
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if isinstance(state, AbortedState):
                concurrent_aborted = True
            elif state.kind in SCHEDULER_TERMINAL_STATE_KINDS:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    f"cannot abort terminal scheduler run in state {state.kind}",
                )
            elif isinstance(
                state, CheckpointPendingState
            ) and should_defer_checkpoint_abort_transition(
                self.store,
                conn,
                run_id=run_id,
                artifacts=self._artifacts,
            ):
                if not self.store.has_abort_requested_for_run(conn, run_id):
                    abort_request = AbortRequestedEvent(run_id=run_id, reason=reason)
                    request_event_id = self._event_id_factory()
                    request_sequence = self.store.next_event_sequence(conn, run_id)
                    self.store.append_event(
                        conn,
                        event_id=request_event_id,
                        run_id=run_id,
                        sequence=request_sequence,
                        event=abort_request,
                        now=now,
                    )
                deferred_checkpoint_abort = True
            else:
                abort_request = AbortRequestedEvent(run_id=run_id, reason=reason)
                request_event_id = self._event_id_factory()
                request_sequence = self.store.next_event_sequence(conn, run_id)
                self.store.append_event(
                    conn,
                    event_id=request_event_id,
                    run_id=run_id,
                    sequence=request_sequence,
                    event=abort_request,
                    now=now,
                )
                aborted_event = RunAbortedEvent(
                    run_id=run_id,
                    reason=reason,
                    prior_state_kind=prior_state_kind,
                )
                now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                abortable_state = cast(
                    SubmittedState
                    | AuthorizedState
                    | AdmittedState
                    | PreflightCompleteState
                    | CursorReadyState
                    | WaitingUsageLimitState
                    | AwaitingCodexReviewState
                    | WaitingCodexReviewRetryState
                    | WaitingForCursorFixState
                    | CheckpointPendingState
                    | AbortedState,
                    state,
                )
                new_state = apply_run_aborted(abortable_state, aborted_event, now_text=now_text)
                aborted_event_id = self._event_id_factory()
                aborted_sequence = self.store.next_event_sequence(conn, run_id)
                self.store.append_event(
                    conn,
                    event_id=aborted_event_id,
                    run_id=run_id,
                    sequence=aborted_sequence,
                    event=aborted_event,
                    now=now,
                )
                if not self.store.compare_and_swap_state(
                    conn,
                    run_id=run_id,
                    expected_version=version,
                    new_state=new_state,
                    now=now,
                ):
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "scheduler run changed during abort",
                    )
                hold = self.store.get_checkpoint_reconciliation_hold_row(conn, run_id)
                if hold is not None and not int(hold["ref_may_have_advanced"]):
                    self.store.release_checkpoint_reconciliation_hold(
                        conn,
                        run_id=run_id,
                        intent_sha256=str(hold["intent_sha256"]),
                    )
                self.store.cancel_live_work(conn, run_id=run_id, now=now)
                self.store.cancel_nonterminal_attempts(
                    conn,
                    run_id=run_id,
                    completion_fence_id=fence_id,
                    now=now,
                )
                if not self.store.has_unresolved_abort_hold(conn, run_id):
                    finalize_aborted_run_cleanup(
                        self.store,
                        conn,
                        run_id=run_id,
                        now=now,
                    )

        if deferred_checkpoint_abort:
            with self.store.begin_read() as conn:
                safe_action = checkpoint_pending_safe_next_action()
            return AbortResult(
                run_id=run_id,
                state_kind="checkpoint_pending",
                abort_persisted=True,
                idempotent_replay=False,
                process_action=AbortProcessAction.NONE,
                termination_pending=False,
                safe_next_action=safe_action,
            )

        return self._reconcile_existing_abort(
            run_id,
            now=now,
            abort_persisted=True,
            idempotent_replay=concurrent_aborted,
        )

    def _reconcile_existing_abort(
        self,
        run_id: str,
        *,
        now: datetime,
        abort_persisted: bool = True,
        idempotent_replay: bool = True,
    ) -> AbortResult:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AbortedState):
                if isinstance(
                    state, CheckpointPendingState
                ) and self.store.has_abort_requested_for_run(conn, run_id):
                    return AbortResult(
                        run_id=run_id,
                        state_kind="checkpoint_pending",
                        abort_persisted=abort_persisted,
                        idempotent_replay=True,
                        process_action=AbortProcessAction.NONE,
                        termination_pending=False,
                        safe_next_action=checkpoint_pending_safe_next_action(),
                    )
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "scheduler run is not aborted",
                )
            pending = self.store.list_pending_abort_attempts(conn, run_id)

        process_action = AbortProcessAction.NONE
        if pending:
            summary = reconcile_run_abort_attempts(
                self.store,
                self.backend,
                run_id=run_id,
                attempt_rows=pending,
                now=now,
                event_id_factory=self._event_id_factory,
                emit_stale_events=False,
            )
            process_action = summary.process_action

        with self.store.begin_immediate() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AbortedState):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "scheduler run changed during abort reconciliation",
                )
            still_pending = self.store.has_unresolved_abort_hold(conn, run_id)
            if not still_pending:
                finalize_aborted_run_cleanup(
                    self.store,
                    conn,
                    run_id=run_id,
                    now=now,
                )

        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            still_pending = self.store.has_unresolved_abort_hold(conn, run_id)
            safe_action = safe_next_action_for_scheduler_state(self.store, conn, state)

        return AbortResult(
            run_id=run_id,
            state_kind="aborted",
            abort_persisted=abort_persisted,
            idempotent_replay=idempotent_replay and not still_pending,
            process_action=process_action,
            termination_pending=still_pending,
            safe_next_action=safe_action,
        )


def default_abort_service(
    *,
    db_path: Path | None = None,
    backend: AgentProcessBackend | None = None,
    artifact_root: Path | None = None,
) -> SchedulerAbortService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return SchedulerAbortService(
        store,
        backend or SystemdUserBackend(),
        artifacts=artifacts,
    )


def scheduler_abort_run(
    run_id: str,
    *,
    reason: str = "user_requested_abort",
    db_path: Path | None = None,
    backend: AgentProcessBackend | None = None,
    artifact_root: Path | None = None,
) -> AbortResult:
    path = db_path or default_engine_db_path()
    if not path.exists():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    return default_abort_service(
        db_path=path,
        backend=backend,
        artifact_root=artifact_root,
    ).abort_run(
        run_id,
        reason=reason,
    )

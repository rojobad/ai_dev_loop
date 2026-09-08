"""Pure scheduler state transitions."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.events import (
    AttemptCompletedEvent,
    AttemptLaunchRequestedEvent,
    AttemptUncertainEvent,
    RunAuthorizedEvent,
    RunSubmittedEvent,
    SyntheticEffectCompletedEvent,
    TickStaleRejectedEvent,
    WorktreeAdmissionBlockedEvent,
    WorktreeAdmittedEvent,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedState,
    AuthorizedState,
    BlockedState,
    SubmittedState,
)


def apply_run_submitted(
    state: SubmittedState,
    event: RunSubmittedEvent,
) -> SubmittedState:
    """Validate that a submitted event matches the queued state."""

    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if event.idempotency_key != state.idempotency_key:
        raise ValueError("event idempotency_key disagrees with state")
    if event.worktree_key != state.context.repository.worktree_key:
        raise ValueError("event worktree_key disagrees with state")
    if state.kind != "queued":
        raise ValueError("run_submitted applies only to queued runs")
    return state


def apply_run_authorized(
    state: SubmittedState | AuthorizedState,
    event: RunAuthorizedEvent,
    *,
    now_text: str,
) -> AuthorizedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if event.controller_session_id != state.context.controller.controller_session_id:
        raise ValueError("event controller_session_id disagrees with frozen context")
    if isinstance(state, AuthorizedState):
        if state.authorized_controller_session_id != event.controller_session_id:
            raise ValueError("authorized controller identity mismatch")
        return state
    if state.kind != "queued":
        raise ValueError("run_authorized applies only to queued runs")
    return AuthorizedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        authorized_at=now_text,
        authorized_controller_session_id=event.controller_session_id,
    )


def apply_worktree_admitted(
    state: AuthorizedState,
    event: WorktreeAdmittedEvent,
    *,
    now_text: str,
) -> AdmittedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if state.kind != "authorized":
        raise ValueError("worktree_admitted applies only to authorized runs")
    return AdmittedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        authorized_at=state.authorized_at,
        authorized_controller_session_id=state.authorized_controller_session_id,
        admitted_at=now_text,
        admission_status_artifact_path=event.admission_status_artifact_path,
        admission_status_sha256=event.admission_status_sha256,
    )


def apply_worktree_admission_blocked(
    state: AuthorizedState,
    event: WorktreeAdmissionBlockedEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if state.kind != "authorized":
        raise ValueError("worktree_admission_blocked applies only to authorized runs")
    return BlockedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        blocked_at=now_text,
        block_reason_kind=event.block_reason_kind,
        block_reason_summary=event.block_reason_summary,
        authorized_at=state.authorized_at,
        authorized_controller_session_id=state.authorized_controller_session_id,
    )


def apply_tick_stale_rejected(
    state: AuthorizedState | AdmittedState,
    event: TickStaleRejectedEvent,
    *,
    now_text: str,
) -> AuthorizedState | AdmittedState:
    """Audit-only stale rejection events must not mutate run state."""

    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    _ = now_text
    return state


def apply_synthetic_effect_completed(
    state: AdmittedState,
    event: SyntheticEffectCompletedEvent,
) -> AdmittedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if state.kind != "admitted":
        raise ValueError("synthetic_effect_completed applies only to admitted runs")
    return state


def apply_attempt_launch_requested(
    state: AdmittedState,
    event: AttemptLaunchRequestedEvent,
) -> AdmittedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return state


def apply_attempt_completed(
    state: AdmittedState,
    event: AttemptCompletedEvent,
) -> AdmittedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return state


def apply_attempt_uncertain(
    state: AdmittedState,
    event: AttemptUncertainEvent,
) -> AdmittedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return state

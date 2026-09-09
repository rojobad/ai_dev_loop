"""Pure scheduler state transitions."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.events import (
    AbortRequestedEvent,
    AttemptCompletedEvent,
    AttemptLaunchRequestedEvent,
    AttemptResultStaleEvent,
    AttemptUncertainEvent,
    AwaitingCodexReviewEnteredEvent,
    CodexBootstrapUncertainEvent,
    CodexReviewBlockedEvent,
    CodexReviewCompletedEvent,
    CodexReviewerBoundEvent,
    CursorChatBlockedEvent,
    CursorChatCreatedEvent,
    CursorTurnBlockedEvent,
    CursorTurnCompletedEvent,
    CursorUsageLimitDetectedEvent,
    MaxIterationsReachedEvent,
    PreflightBlockedEvent,
    PreflightCompletedEvent,
    RunAbortedEvent,
    RunAuthorizedEvent,
    RunCompletedEvent,
    RunCompletedWithResidualRiskEvent,
    RunSubmittedEvent,
    StagingBlockedEvent,
    StagingCompletedEvent,
    SyntheticEffectCompletedEvent,
    TickStaleRejectedEvent,
    WaitingForCursorFixEnteredEvent,
    WorktreeAdmissionBlockedEvent,
    WorktreeAdmittedEvent,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_ABORTABLE_STATE_KINDS,
    SCHEDULER_TERMINAL_STATE_KINDS,
    AbortedState,
    AdmittedRunCheckpoint,
    AdmittedState,
    AuthorizedState,
    AwaitingCodexReviewState,
    BlockedState,
    CodexWorkflowCheckpoint,
    CompletedState,
    CompletedWithResidualRiskState,
    CursorReadyState,
    CursorWorkflowCheckpoint,
    MaxIterationsReachedState,
    PreflightCompleteState,
    SubmittedState,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
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


def _admitted_checkpoint(state: AdmittedState) -> AdmittedRunCheckpoint:
    return AdmittedRunCheckpoint(
        authorized_at=state.authorized_at,
        authorized_controller_session_id=state.authorized_controller_session_id,
        admitted_at=state.admitted_at,
        admission_status_artifact_path=state.admission_status_artifact_path,
        admission_status_sha256=state.admission_status_sha256,
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


def apply_preflight_completed(
    state: AdmittedState,
    event: PreflightCompletedEvent,
    *,
    now_text: str,
) -> PreflightCompleteState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if state.kind != "admitted":
        raise ValueError("preflight_completed applies only to admitted runs")
    return PreflightCompleteState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=_admitted_checkpoint(state),
        cursor=CursorWorkflowCheckpoint(),
    )


def apply_preflight_blocked(
    state: AdmittedState,
    event: PreflightBlockedEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
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


def apply_cursor_chat_created(
    state: PreflightCompleteState,
    event: CursorChatCreatedEvent,
    *,
    now_text: str,
) -> CursorReadyState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    cursor = state.cursor.model_copy(
        update={
            "chat_id": event.chat_id,
            "chat_artifact_path": event.chat_artifact_path,
            "chat_artifact_sha256": event.chat_artifact_sha256,
            "original_prompt_path": state.context.plan_prompt.prompt_artifact_path,
            "original_prompt_sha256": state.context.plan_prompt.prompt_sha256,
        }
    )
    return CursorReadyState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=cursor,
    )


def apply_cursor_chat_blocked(
    state: PreflightCompleteState,
    event: CursorChatBlockedEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
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
        authorized_at=state.checkpoint.authorized_at,
        authorized_controller_session_id=state.checkpoint.authorized_controller_session_id,
    )


def apply_cursor_turn_completed(
    state: CursorReadyState | WaitingUsageLimitState | WaitingForCursorFixState,
    event: CursorTurnCompletedEvent,
    *,
    now_text: str,
) -> CursorReadyState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    cursor = state.cursor.model_copy(
        update={
            "iteration": event.iteration,
            "cursor_output_fingerprint_path": event.cursor_output_fingerprint_path,
            "cursor_output_fingerprint_sha256": event.cursor_output_fingerprint_sha256,
            "wait_until": None,
        }
    )
    return CursorReadyState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=cursor,
        codex=getattr(state, "codex", CodexWorkflowCheckpoint()),
    )


def apply_cursor_turn_blocked(
    state: CursorReadyState | WaitingUsageLimitState | WaitingForCursorFixState,
    event: CursorTurnBlockedEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
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
        authorized_at=state.checkpoint.authorized_at,
        authorized_controller_session_id=state.checkpoint.authorized_controller_session_id,
    )


def apply_cursor_usage_limit_detected(
    state: CursorReadyState | WaitingForCursorFixState,
    event: CursorUsageLimitDetectedEvent,
    *,
    now_text: str,
) -> WaitingUsageLimitState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    cursor_updates: dict[str, object] = {
        "iteration": event.iteration,
        "wait_until": event.wait_until,
        "usage_limit_fingerprint_path": event.usage_limit_fingerprint_path,
        "usage_limit_fingerprint_sha256": event.usage_limit_fingerprint_sha256,
        "continuation_envelope_path": event.continuation_envelope_path,
        "continuation_envelope_sha256": event.continuation_envelope_sha256,
    }
    if isinstance(state, WaitingForCursorFixState):
        cursor_updates["original_prompt_path"] = state.codex.latest_fix_prompt_path
        cursor_updates["original_prompt_sha256"] = state.codex.latest_fix_prompt_sha256
    cursor = state.cursor.model_copy(update=cursor_updates)
    return WaitingUsageLimitState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=cursor,
        codex=getattr(state, "codex", CodexWorkflowCheckpoint()),
    )


def apply_staging_completed(
    state: CursorReadyState,
    event: StagingCompletedEvent,
    *,
    now_text: str,
) -> AwaitingCodexReviewState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    cursor = state.cursor.model_copy(
        update={
            "iteration": event.iteration,
            "staged_patch_path": event.staged_patch_path,
            "staged_patch_sha256": event.staged_patch_sha256,
        }
    )
    return AwaitingCodexReviewState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=cursor,
        codex=getattr(state, "codex", CodexWorkflowCheckpoint()),
    )


def apply_staging_blocked(
    state: CursorReadyState,
    event: StagingBlockedEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
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
        authorized_at=state.checkpoint.authorized_at,
        authorized_controller_session_id=state.checkpoint.authorized_controller_session_id,
    )


def apply_awaiting_codex_review_entered(
    state: AwaitingCodexReviewState,
    event: AwaitingCodexReviewEnteredEvent,
) -> AwaitingCodexReviewState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return state


def apply_codex_reviewer_bound(
    state: AwaitingCodexReviewState,
    event: CodexReviewerBoundEvent,
    *,
    now_text: str,
    reviewer_session_id: str,
) -> AwaitingCodexReviewState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    codex = state.codex.model_copy(
        update={
            "reviewer_session_id": reviewer_session_id,
            "binding_artifact_path": event.binding_artifact_path,
            "binding_artifact_sha256": event.binding_artifact_sha256,
            "bootstrap_uncertainty_reason": None,
        }
    )
    return AwaitingCodexReviewState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=state.cursor,
        codex=codex,
    )


def apply_codex_bootstrap_uncertain(
    state: AwaitingCodexReviewState,
    event: CodexBootstrapUncertainEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return BlockedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        blocked_at=now_text,
        block_reason_kind="codex_bootstrap_uncertain",
        block_reason_summary=f"Codex reviewer bootstrap is uncertain: {event.uncertainty_reason}",
        authorized_at=state.checkpoint.authorized_at,
        authorized_controller_session_id=state.checkpoint.authorized_controller_session_id,
    )


def apply_codex_review_blocked(
    state: AwaitingCodexReviewState,
    event: CodexReviewBlockedEvent,
    *,
    now_text: str,
) -> BlockedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
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
        authorized_at=state.checkpoint.authorized_at,
        authorized_controller_session_id=state.checkpoint.authorized_controller_session_id,
    )


def apply_codex_review_completed(
    state: AwaitingCodexReviewState,
    event: CodexReviewCompletedEvent,
    *,
    now_text: str,
) -> AwaitingCodexReviewState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    codex = state.codex.model_copy(
        update={
            "review_iteration": event.review_iteration,
            "reviews_completed": state.codex.reviews_completed + 1,
            "latest_review_result_path": event.review_result_path,
            "latest_review_result_sha256": event.review_result_sha256,
        }
    )
    return AwaitingCodexReviewState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=state.cursor,
        codex=codex,
    )


def apply_waiting_for_cursor_fix_entered(
    state: AwaitingCodexReviewState,
    event: WaitingForCursorFixEnteredEvent,
    *,
    now_text: str,
) -> WaitingForCursorFixState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    codex = state.codex.model_copy(
        update={
            "latest_fix_prompt_path": event.fix_prompt_path,
            "latest_fix_prompt_sha256": event.fix_prompt_sha256,
            "latest_correction_envelope_path": event.correction_envelope_path,
            "latest_correction_envelope_sha256": event.correction_envelope_sha256,
            "review_iteration": event.review_iteration,
            "reviews_completed": state.codex.reviews_completed + 1,
        }
    )
    cursor = state.cursor.model_copy(
        update={
            "iteration": event.review_iteration + 1,
            "usage_limit_fingerprint_path": None,
            "usage_limit_fingerprint_sha256": None,
            "continuation_envelope_path": None,
            "continuation_envelope_sha256": None,
            "wait_until": None,
        }
    )
    return WaitingForCursorFixState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=cursor,
        codex=codex,
    )


def apply_run_completed(
    state: AwaitingCodexReviewState,
    event: RunCompletedEvent,
    *,
    now_text: str,
) -> CompletedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    codex = state.codex.model_copy(
        update={
            "review_iteration": event.review_iteration,
            "reviews_completed": state.codex.reviews_completed + 1,
        }
    )
    return CompletedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=state.cursor,
        codex=codex,
    )


def apply_run_completed_with_residual_risk(
    state: AwaitingCodexReviewState,
    event: RunCompletedWithResidualRiskEvent,
    *,
    now_text: str,
) -> CompletedWithResidualRiskState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    codex = state.codex.model_copy(
        update={
            "review_iteration": event.review_iteration,
            "reviews_completed": state.codex.reviews_completed + 1,
        }
    )
    return CompletedWithResidualRiskState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=state.cursor,
        codex=codex,
    )


def apply_max_iterations_reached(
    state: AwaitingCodexReviewState,
    event: MaxIterationsReachedEvent,
    *,
    now_text: str,
) -> MaxIterationsReachedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    codex = state.codex.model_copy(
        update={
            "review_iteration": event.review_iteration,
            "reviews_completed": state.codex.reviews_completed + 1,
        }
    )
    return MaxIterationsReachedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=state.checkpoint,
        cursor=state.cursor,
        codex=codex,
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


def _abort_checkpoint_from_state(
    state: object,
) -> tuple[
    AdmittedRunCheckpoint | None,
    CursorWorkflowCheckpoint | None,
    CodexWorkflowCheckpoint | None,
    str | None,
    str | None,
]:
    checkpoint = getattr(state, "checkpoint", None)
    cursor = getattr(state, "cursor", None)
    codex = getattr(state, "codex", None)
    authorized_at = getattr(state, "authorized_at", None)
    authorized_controller_session_id = getattr(state, "authorized_controller_session_id", None)
    if isinstance(state, AdmittedState) and checkpoint is None:
        checkpoint = AdmittedRunCheckpoint(
            authorized_at=state.authorized_at,
            authorized_controller_session_id=state.authorized_controller_session_id,
            admitted_at=state.admitted_at,
            admission_status_artifact_path=state.admission_status_artifact_path,
            admission_status_sha256=state.admission_status_sha256,
        )
    return checkpoint, cursor, codex, authorized_at, authorized_controller_session_id


def apply_run_aborted(
    state: (
        SubmittedState
        | AuthorizedState
        | AdmittedState
        | PreflightCompleteState
        | CursorReadyState
        | WaitingUsageLimitState
        | AwaitingCodexReviewState
        | WaitingForCursorFixState
        | AbortedState
    ),
    event: RunAbortedEvent,
    *,
    now_text: str,
) -> AbortedState:
    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if isinstance(state, AbortedState):
        if state.abort_reason != event.reason or state.prior_state_kind != event.prior_state_kind:
            raise ValueError("aborted state disagrees with event")
        return state
    if state.kind in SCHEDULER_TERMINAL_STATE_KINDS:
        raise ValueError("run_aborted applies only to non-terminal runs")
    if state.kind not in SCHEDULER_ABORTABLE_STATE_KINDS:
        raise ValueError("run_aborted applies only to abortable runs")
    checkpoint, cursor, codex, authorized_at, authorized_controller_session_id = (
        _abort_checkpoint_from_state(state)
    )
    return AbortedState(
        run_id=state.run_id,
        version=state.version + 1,
        submitted_at=state.submitted_at,
        updated_at=now_text,
        idempotency_key=state.idempotency_key,
        context=state.context,
        aborted_at=now_text,
        abort_reason=event.reason,
        prior_state_kind=event.prior_state_kind,
        checkpoint=checkpoint,
        cursor=cursor,
        codex=codex,
        authorized_at=authorized_at,
        authorized_controller_session_id=authorized_controller_session_id,
    )


def apply_abort_requested(
    state: (
        SubmittedState
        | AuthorizedState
        | AdmittedState
        | PreflightCompleteState
        | CursorReadyState
        | WaitingUsageLimitState
        | AwaitingCodexReviewState
        | WaitingForCursorFixState
        | AbortedState
    ),
    event: AbortRequestedEvent,
) -> (
    SubmittedState
    | AuthorizedState
    | AdmittedState
    | PreflightCompleteState
    | CursorReadyState
    | WaitingUsageLimitState
    | AwaitingCodexReviewState
    | WaitingForCursorFixState
    | AbortedState
):
    """Abort request is durable evidence only until run_aborted is applied."""

    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return state


def apply_attempt_result_stale(
    state: (
        AdmittedState
        | PreflightCompleteState
        | CursorReadyState
        | WaitingUsageLimitState
        | AwaitingCodexReviewState
        | WaitingForCursorFixState
        | AbortedState
    ),
    event: AttemptResultStaleEvent,
) -> (
    AdmittedState
    | PreflightCompleteState
    | CursorReadyState
    | WaitingUsageLimitState
    | AwaitingCodexReviewState
    | WaitingForCursorFixState
    | AbortedState
):
    """Late attempt evidence after abort/cancellation must not mutate run state."""

    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    return state

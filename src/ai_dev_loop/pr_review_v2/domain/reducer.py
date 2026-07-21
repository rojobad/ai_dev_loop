"""Pure deterministic reducer for PR review v2."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final

from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    ArtifactRef,
    DomainModel,
    ErrorSummary,
    ExistingPrOrigin,
    GitSha40,
    LocalFixOutcomeKind,
    NonEmptyId,
    NonEmptyStr,
    PauseReasonKind,
    PublicationStep,
    PullRequestBinding,
    ReconciliationResolutionKind,
    RejectionCode,
    ReplyIntent,
    RepositoryIdentity,
    SafeAction,
    SafeActionKind,
    SourceRunOrigin,
    build_effect_identity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    CommitPatchEffect,
    CreateOrUpdatePrEffect,
    GeneratePublicationTextEffect,
    ObserveBotReviewEffect,
    PostThreadReplyEffect,
    PrReviewEffect,
    PushCommitEffect,
    ReconcileWriteEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    RunLocalFixEffect,
    UpdatePrTextEffect,
    is_mutating_effect,
    stable_effect_ids,
    strategy_for_mutating_effect,
    with_attempt,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    AbortRequested,
    AdjudicationRecordedOutcome,
    BotStillWaitingOutcome,
    CommitRecordedOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    EffectSuccessOutcome,
    EligibleThreadsObservedOutcome,
    FatalFailureDetected,
    LocalFixFinishedOutcome,
    PrBoundOutcome,
    PrReviewEvent,
    PrTextUpdatedOutcome,
    PublicationTextPreparedOutcome,
    PushConfirmedOutcome,
    ReconciliationResolvedOutcome,
    ResumeRequested,
    RetryDue,
    ReviewTriggerConfirmedOutcome,
    StartRequested,
    ThreadReplyConfirmedOutcome,
    ThreadResolutionConfirmedOutcome,
    UserContinuationRequested,
    VerifiedNoFindingsOutcome,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.domain.state import (
    TERMINAL_STATE_KINDS,
    AbortedState,
    AdjudicatingState,
    CompletedState,
    FailedState,
    PausedState,
    PreparedState,
    PrReviewState,
    PublishingFixState,
    PublishingInitialState,
    ReconcilingWriteState,
    ResumableContinuation,
    RetrySuspendableState,
    RunningLocalFixState,
    WaitingForBotState,
    WaitingForUserState,
    WaitingRetryState,
    active_effect,
    binding_of,
)


class TransitionApplied(DomainModel):
    state: PrReviewState
    effects: tuple[PrReviewEffect, ...]


class TransitionRejected(DomainModel):
    code: RejectionCode
    state_kind: NonEmptyStr
    event_kind: NonEmptyStr
    effect_id: NonEmptyId | None = None
    detail: NonEmptyStr | None = None


TransitionResult = TransitionApplied | TransitionRejected
Handler = Callable[[PrReviewState, PrReviewEvent], TransitionResult]

EVENT_KINDS: Final[tuple[str, ...]] = (
    "start_requested",
    "effect_succeeded",
    "effect_retryable_failure",
    "effect_blocked",
    "write_outcome_uncertain",
    "retry_due",
    "resume_requested",
    "user_continuation_requested",
    "abort_requested",
    "fatal_failure_detected",
)

STATE_KINDS: Final[tuple[str, ...]] = (
    "prepared",
    "publishing_initial",
    "waiting_for_bot",
    "adjudicating",
    "waiting_for_user",
    "running_local_fix",
    "publishing_fix",
    "reconciling_write",
    "waiting_retry",
    "completed",
    "paused",
    "failed",
    "aborted",
)

OUTCOME_FOR_EFFECT: Final[Mapping[str, frozenset[str]]] = {
    "generate_publication_text": frozenset({"publication_text_prepared"}),
    "commit_patch": frozenset({"commit_recorded"}),
    "push_commit": frozenset({"push_confirmed"}),
    "create_or_update_pr": frozenset({"pr_bound"}),
    "request_bot_review": frozenset({"review_trigger_confirmed"}),
    "observe_bot_review": frozenset(
        {"bot_still_waiting", "verified_no_findings", "eligible_threads_observed"}
    ),
    "adjudicate_threads": frozenset({"adjudication_recorded"}),
    "post_thread_reply": frozenset({"thread_reply_confirmed"}),
    "run_local_fix": frozenset({"local_fix_finished"}),
    "update_pr_text": frozenset({"pr_text_updated"}),
    "resolve_thread": frozenset({"thread_resolution_confirmed"}),
    "reconcile_write": frozenset({"reconciliation_resolved"}),
}


def _reject(
    state: PrReviewState,
    event: PrReviewEvent,
    code: RejectionCode,
    *,
    effect_id: str | None = None,
    detail: str | None = None,
) -> TransitionRejected:
    return TransitionRejected(
        code=code,
        state_kind=state.kind,
        event_kind=event.kind,
        effect_id=effect_id,
        detail=detail,
    )


def _apply(state: PrReviewState, *effects: PrReviewEffect) -> TransitionApplied:
    return TransitionApplied(state=state, effects=tuple(effects))


def _validate_token(
    state: PrReviewState,
    event: PrReviewEvent,
    token_event: EffectSucceeded | EffectRetryableFailure | EffectBlocked | WriteOutcomeUncertain,
) -> TransitionRejected | PrReviewEffect:
    effect = active_effect(state)
    if effect is None:
        return _reject(state, event, RejectionCode.NO_ACTIVE_EFFECT)
    token = token_event.token
    if token.expected_run_version < 1 or token.lease_generation < 1:
        return _reject(state, event, RejectionCode.INVALID_TOKEN_SHAPE, effect_id=effect.effect_id)
    if token.effect_id != effect.effect_id:
        return _reject(state, event, RejectionCode.STALE_EFFECT_ID, effect_id=token.effect_id)
    if token.cycle_number != effect.cycle_number or token.cycle_number != getattr(
        state, "cycle_number", effect.cycle_number
    ):
        return _reject(state, event, RejectionCode.STALE_CYCLE, effect_id=effect.effect_id)
    if token.bound_head_sha != effect.bound_head_sha:
        return _reject(state, event, RejectionCode.STALE_HEAD_SHA, effect_id=effect.effect_id)
    return effect


def _abort(state: PrReviewState, event: AbortRequested) -> TransitionApplied:
    return _apply(
        AbortedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            reason=event.reason,
            aborted_at=event.occurred_at,
            binding=binding_of(state),
        )
    )


def _fail(state: PrReviewState, event: FatalFailureDetected) -> TransitionApplied:
    return _apply(
        FailedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            reason=event.reason,
            safe_summary=event.safe_summary,
            failed_at=event.occurred_at,
            binding=binding_of(state),
        )
    )


def _execution_context_ref(state: PrReviewState) -> ArtifactRef:
    return state.origin.execution_context_ref


def _make_generate_publication_text(
    *,
    run_id: str,
    cycle: int,
    repository: RepositoryIdentity,
    head_sha: GitSha40,
    evidence_ref: ArtifactRef,
    patch_ref: ArtifactRef,
    max_attempts: int,
) -> GeneratePublicationTextEffect:
    effect_id, idem = stable_effect_ids(
        run_id=run_id, cycle_number=cycle, operation="generate_publication_text"
    )
    return GeneratePublicationTextEffect(
        effect_id=effect_id,
        idempotency_key=idem,
        run_id=run_id,
        cycle_number=cycle,
        attempt=1,
        max_attempts=max_attempts,
        repository=repository,
        bound_head_sha=head_sha,
        evidence_ref=evidence_ref,
        patch_ref=patch_ref,
    )


def _make_request_bot_review(
    *,
    run_id: str,
    cycle: int,
    binding: PullRequestBinding,
    max_attempts: int,
) -> RequestBotReviewEffect:
    marker = build_effect_identity(run_id=run_id, cycle_number=cycle, operation="review-trigger")
    effect_id, idem = stable_effect_ids(
        run_id=run_id, cycle_number=cycle, operation="request_bot_review"
    )
    return RequestBotReviewEffect(
        effect_id=effect_id,
        idempotency_key=idem,
        run_id=run_id,
        cycle_number=cycle,
        attempt=1,
        max_attempts=max_attempts,
        repository=binding.repository,
        bound_head_sha=binding.head_sha,
        binding=binding,
        marker=marker,
    )


def _make_observe(
    *,
    run_id: str,
    cycle: int,
    binding: PullRequestBinding,
    poll_sequence: int,
    trigger_marker: str,
    max_attempts: int,
    not_before: datetime | None = None,
) -> ObserveBotReviewEffect:
    effect_id, idem = stable_effect_ids(
        run_id=run_id,
        cycle_number=cycle,
        operation="observe_bot_review",
        target=f"poll-{poll_sequence:04d}",
    )
    return ObserveBotReviewEffect(
        effect_id=effect_id,
        idempotency_key=idem,
        run_id=run_id,
        cycle_number=cycle,
        attempt=1,
        max_attempts=max_attempts,
        repository=binding.repository,
        bound_head_sha=binding.head_sha,
        binding=binding,
        poll_sequence=poll_sequence,
        not_before=not_before,
        trigger_marker=trigger_marker,
    )


def _handle_start(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(state, PreparedState)
    assert isinstance(event, StartRequested)
    max_attempts = state.limits.github_max_attempts_per_batch
    if isinstance(state.origin, SourceRunOrigin):
        effect = _make_generate_publication_text(
            run_id=state.run_id,
            cycle=1,
            repository=state.origin.repository,
            head_sha=state.origin.expected_head_sha,
            evidence_ref=state.origin.execution_context_ref,
            patch_ref=state.origin.accepted_patch,
            max_attempts=max_attempts,
        )
        new_state = PublishingInitialState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=1,
            entered_at=event.occurred_at,
            step=PublicationStep.GENERATE_PUBLICATION_TEXT,
            active_effect=effect,
        )
        return _apply(new_state, effect)
    if isinstance(state.origin, ExistingPrOrigin):
        request = _make_request_bot_review(
            run_id=state.run_id,
            cycle=1,
            binding=state.origin.binding,
            max_attempts=max_attempts,
        )
        waiting = WaitingForBotState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            binding=state.origin.binding,
            cycle_number=1,
            entered_at=event.occurred_at,
            poll_sequence=1,
            active_effect=request,
            trigger_evidence=None,
        )
        return _apply(waiting, request)
    return _reject(state, event, RejectionCode.INVALID_ORIGIN_FOR_EVENT)


def _to_waiting_retry(
    state: RetrySuspendableState,
    effect: PrReviewEffect,
    *,
    next_attempt: int,
    next_attempt_at: datetime,
    error: ErrorSummary,
    occurred_at: datetime,
) -> TransitionApplied:
    updated_effect = with_attempt(effect, next_attempt)
    suspended = _replace_active_effect(state, updated_effect)
    return _apply(
        WaitingRetryState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            entered_at=occurred_at,
            suspended=suspended,
            retrying_effect_id=updated_effect.effect_id,
            attempt=next_attempt,
            max_attempts=updated_effect.max_attempts,
            next_attempt_at=next_attempt_at,
            last_error=error,
            binding=binding_of(state),
        )
    )


def _to_paused_retry_exhausted(
    state: RetrySuspendableState,
    effect: PrReviewEffect,
    *,
    error: ErrorSummary,
    occurred_at: datetime,
) -> TransitionApplied:
    reset = with_attempt(effect, 1)
    resumable = _replace_active_effect(state, reset)
    return _apply(
        PausedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            reason=PauseReasonKind.RETRY_EXHAUSTED,
            safe_action=SafeAction(
                kind=SafeActionKind.RESUME_SAME_EFFECT,
                condition="operator resumes after retry budget exhaustion",
            ),
            safe_summary=error.safe_summary,
            paused_at=occurred_at,
            binding=binding_of(state),
            resumable=resumable,
        )
    )


def _replace_active_effect(
    state: RetrySuspendableState, effect: PrReviewEffect
) -> ResumableContinuation:
    if isinstance(state, ReconcilingWriteState):
        assert isinstance(effect, ReconcileWriteEffect)
        return state.model_copy(update={"active_effect": effect})
    if isinstance(state, WaitingForUserState):
        assert isinstance(effect, PostThreadReplyEffect) or effect is None
        return state.model_copy(update={"active_effect": effect})
    if isinstance(state, PublishingFixState):
        return state.model_copy(update={"active_effect": effect})
    return state.model_copy(update={"active_effect": effect})


def _handle_retryable_failure(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, EffectRetryableFailure)
    if state.kind in {"prepared", "paused", "waiting_retry"} or state.kind in TERMINAL_STATE_KINDS:
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    validated = _validate_token(state, event, event)
    if isinstance(validated, TransitionRejected):
        return validated
    effect = validated
    if event.failed_attempt != effect.attempt:
        return _reject(
            state,
            event,
            RejectionCode.MISMATCHED_ATTEMPT,
            effect_id=effect.effect_id,
        )
    assert isinstance(
        state,
        PublishingInitialState
        | WaitingForBotState
        | AdjudicatingState
        | WaitingForUserState
        | RunningLocalFixState
        | PublishingFixState
        | ReconcilingWriteState,
    )
    if effect.attempt >= effect.max_attempts:
        return _to_paused_retry_exhausted(
            state,
            effect,
            error=event.error,
            occurred_at=event.occurred_at,
        )
    return _to_waiting_retry(
        state,
        effect,
        next_attempt=effect.attempt + 1,
        next_attempt_at=event.next_attempt_at,
        error=event.error,
        occurred_at=event.occurred_at,
    )


def _handle_blocked(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, EffectBlocked)
    if state.kind in {"prepared", "paused", "waiting_retry"} or state.kind in TERMINAL_STATE_KINDS:
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    validated = _validate_token(state, event, event)
    if isinstance(validated, TransitionRejected):
        return validated
    resumable: ResumableContinuation | None
    if isinstance(
        state,
        PublishingInitialState
        | WaitingForBotState
        | AdjudicatingState
        | WaitingForUserState
        | RunningLocalFixState
        | PublishingFixState
        | ReconcilingWriteState,
    ):
        resumable = state
    else:
        resumable = None
    return _apply(
        PausedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            reason=event.reason,
            safe_action=event.safe_action,
            safe_summary=event.safe_summary,
            paused_at=event.occurred_at,
            binding=binding_of(state),
            resumable=resumable,
        )
    )


def _handle_uncertain(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, WriteOutcomeUncertain)
    if (
        state.kind
        in {
            "prepared",
            "paused",
            "waiting_retry",
            "reconciling_write",
        }
        or state.kind in TERMINAL_STATE_KINDS
    ):
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    validated = _validate_token(state, event, event)
    if isinstance(validated, TransitionRejected):
        return validated
    effect = validated
    if not is_mutating_effect(effect):
        return _reject(
            state,
            event,
            RejectionCode.WRITE_UNCERTAIN_FOR_NON_MUTATING,
            effect_id=effect.effect_id,
        )
    assert isinstance(
        effect,
        (
            CommitPatchEffect
            | PushCommitEffect
            | CreateOrUpdatePrEffect
            | RequestBotReviewEffect
            | PostThreadReplyEffect
            | UpdatePrTextEffect
            | ResolveThreadEffect,
        ),
    )
    if event.original_write != effect:
        return _reject(
            state,
            event,
            RejectionCode.MISMATCHED_ORIGINAL_WRITE,
            effect_id=effect.effect_id,
            detail="WriteOutcomeUncertain.original_write must fully equal the active effect",
        )
    assert isinstance(
        state,
        PublishingInitialState
        | WaitingForBotState
        | AdjudicatingState
        | WaitingForUserState
        | RunningLocalFixState
        | PublishingFixState,
    )
    reconcile_id, reconcile_idem = stable_effect_ids(
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        operation="reconcile_write",
        target=effect.effect_id,
    )
    reconcile = ReconcileWriteEffect(
        effect_id=reconcile_id,
        idempotency_key=reconcile_idem,
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        attempt=1,
        max_attempts=state.limits.github_max_attempts_per_batch,
        repository=effect.repository,
        bound_head_sha=effect.bound_head_sha,
        original_write=effect,
        strategy=strategy_for_mutating_effect(effect),
        reconciliation_identity=event.reconciliation_identity,
    )
    return _apply(
        ReconcilingWriteState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            entered_at=event.occurred_at,
            suspended=state,
            original_write=effect,
            active_effect=reconcile,
            last_error=event.error,
            binding=binding_of(state),
        ),
        reconcile,
    )


def _handle_retry_due(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, RetryDue)
    if not isinstance(state, WaitingRetryState):
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    if event.pending_effect_id != state.retrying_effect_id:
        return _reject(
            state,
            event,
            RejectionCode.INVALID_RETRY_TARGET,
            effect_id=event.pending_effect_id,
        )
    if event.current_time < state.next_attempt_at:
        return _reject(state, event, RejectionCode.EARLY_RETRY_TIMER)
    effect = active_effect(state)
    if effect is None:
        return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
    restored = state.suspended
    return _apply(restored, effect)


def _handle_resume(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, ResumeRequested)
    if not isinstance(state, PausedState):
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    if state.resumable is None:
        return _reject(state, event, RejectionCode.RESUME_WITHOUT_CONTINUATION)
    effect = active_effect(state.resumable)
    if effect is None:
        return _reject(state, event, RejectionCode.RESUME_WITHOUT_CONTINUATION)
    reset = with_attempt(effect, 1)
    restored = _replace_active_effect(state.resumable, reset)
    return _apply(restored, reset)


def _handle_user_continuation(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, UserContinuationRequested)
    if not isinstance(state, WaitingForUserState):
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    if state.active_effect is not None or state.remaining_replies:
        return _reject(state, event, RejectionCode.USER_CONTINUATION_BEFORE_REPLIES)
    evidence = event.evidence
    if (
        evidence.repository != state.binding.repository
        or evidence.pr_number != state.binding.pr_number
        or evidence.cycle_number != state.cycle_number
        or evidence.head_sha != state.binding.head_sha
    ):
        return _reject(state, event, RejectionCode.INVALID_USER_CONTINUATION)
    if state.trigger_evidence is None:
        return _reject(state, event, RejectionCode.MISSING_TRIGGER_EVIDENCE)
    observe = _make_observe(
        run_id=state.run_id,
        cycle=state.cycle_number,
        binding=state.binding,
        poll_sequence=1,
        trigger_marker=state.trigger_evidence.marker,
        max_attempts=state.limits.github_max_attempts_per_batch,
    )
    new_state = WaitingForBotState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=state.binding,
        cycle_number=state.cycle_number,
        entered_at=event.occurred_at,
        poll_sequence=1,
        active_effect=observe,
        trigger_evidence=state.trigger_evidence,
    )
    return _apply(new_state, observe)


def _handle_success_publishing_initial(
    state: PublishingInitialState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    outcome = event.outcome
    max_attempts = state.limits.github_max_attempts_per_batch
    if effect.kind == "generate_publication_text":
        if not isinstance(outcome, PublicationTextPreparedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        commit_id, commit_idem = stable_effect_ids(
            run_id=state.run_id, cycle_number=state.cycle_number, operation="commit_patch"
        )
        commit = CommitPatchEffect(
            effect_id=commit_id,
            idempotency_key=commit_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=state.origin.repository,
            bound_head_sha=state.origin.expected_head_sha,
            patch_ref=state.origin.accepted_patch,
            expected_head_sha=state.origin.expected_head_sha,
            commit_message_ref=outcome.commit_message_ref,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.COMMIT_PATCH,
                "active_effect": commit,
                "publication_text_ref": outcome.publication_text_ref,
                "commit_message_ref": outcome.commit_message_ref,
            }
        )
        return _apply(new_state, commit)
    if effect.kind == "commit_patch":
        if not isinstance(outcome, CommitRecordedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, CommitPatchEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if outcome.commit_sha != outcome.new_head_sha:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="commit_sha must equal new_head_sha",
            )
        if outcome.new_head_sha == effect.expected_head_sha:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="new_head_sha must advance past expected_head_sha",
            )
        push_id, push_idem = stable_effect_ids(
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            operation="push_commit",
            target=outcome.commit_sha,
        )
        push = PushCommitEffect(
            effect_id=push_id,
            idempotency_key=push_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=state.origin.repository,
            bound_head_sha=outcome.new_head_sha,
            commit_sha=outcome.commit_sha,
            remote_ref=state.origin.head_branch,
            force=False,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.PUSH_COMMIT,
                "active_effect": push,
                "commit_sha": outcome.commit_sha,
            }
        )
        return _apply(new_state, push)
    if effect.kind == "push_commit":
        if not isinstance(outcome, PushConfirmedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, PushCommitEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if outcome.commit_sha != effect.commit_sha or outcome.remote_ref != effect.remote_ref:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="push confirmation must match effect commit_sha and remote_ref",
            )
        if state.publication_text_ref is None or state.commit_sha is None:
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if state.commit_sha != effect.commit_sha:
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        pr_id, pr_idem = stable_effect_ids(
            run_id=state.run_id, cycle_number=state.cycle_number, operation="create_or_update_pr"
        )
        create_pr = CreateOrUpdatePrEffect(
            effect_id=pr_id,
            idempotency_key=pr_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=state.origin.repository,
            bound_head_sha=state.commit_sha,
            head_branch=state.origin.head_branch,
            base_branch=state.origin.base_branch,
            publication_text_ref=state.publication_text_ref,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.CREATE_OR_UPDATE_PR,
                "active_effect": create_pr,
            }
        )
        return _apply(new_state, create_pr)
    if effect.kind == "create_or_update_pr":
        if not isinstance(outcome, PrBoundOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, CreateOrUpdatePrEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        binding = outcome.binding
        if (
            binding.repository != effect.repository
            or binding.head_branch != effect.head_branch
            or binding.base_branch != effect.base_branch
            or binding.head_sha != effect.bound_head_sha
        ):
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="PR binding must match create_or_update_pr preconditions",
            )
        request = _make_request_bot_review(
            run_id=state.run_id,
            cycle=state.cycle_number,
            binding=binding,
            max_attempts=max_attempts,
        )
        waiting = WaitingForBotState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            binding=binding,
            cycle_number=state.cycle_number,
            entered_at=event.occurred_at,
            poll_sequence=1,
            active_effect=request,
            trigger_evidence=None,
        )
        return _apply(waiting, request)
    return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)


def _handle_success_waiting_for_bot(
    state: WaitingForBotState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    outcome = event.outcome
    max_attempts = state.limits.github_max_attempts_per_batch
    if effect.kind == "request_bot_review":
        if not isinstance(outcome, ReviewTriggerConfirmedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, RequestBotReviewEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        evidence = outcome.evidence
        if evidence.marker != effect.marker:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="trigger marker must match request effect marker",
            )
        if (
            evidence.head_sha != state.binding.head_sha
            or evidence.head_sha != effect.bound_head_sha
        ):
            return _reject(state, event, RejectionCode.STALE_HEAD_SHA)
        if effect.binding != state.binding:
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_BINDING)
        observe = _make_observe(
            run_id=state.run_id,
            cycle=state.cycle_number,
            binding=state.binding,
            poll_sequence=1,
            trigger_marker=evidence.marker,
            max_attempts=max_attempts,
        )
        new_state = state.model_copy(
            update={
                "active_effect": observe,
                "trigger_evidence": evidence,
                "poll_sequence": 1,
            }
        )
        return _apply(new_state, observe)
    if effect.kind == "observe_bot_review":
        if not isinstance(effect, ObserveBotReviewEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if effect.poll_sequence != state.poll_sequence or effect.binding != state.binding:
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if isinstance(outcome, BotStillWaitingOutcome):
            expected_seq = state.poll_sequence + 1
            if outcome.poll_sequence != expected_seq:
                return _reject(
                    state,
                    event,
                    RejectionCode.INVALID_POLL_SEQUENCE,
                    effect_id=effect.effect_id,
                    detail=f"expected poll_sequence {expected_seq}",
                )
            if state.trigger_evidence is None:
                return _reject(state, event, RejectionCode.MISSING_TRIGGER_EVIDENCE)
            observe = _make_observe(
                run_id=state.run_id,
                cycle=state.cycle_number,
                binding=state.binding,
                poll_sequence=expected_seq,
                trigger_marker=state.trigger_evidence.marker,
                max_attempts=max_attempts,
                not_before=outcome.next_not_before,
            )
            new_state = state.model_copy(
                update={"active_effect": observe, "poll_sequence": expected_seq}
            )
            return _apply(new_state, observe)
        if isinstance(outcome, VerifiedNoFindingsOutcome):
            if outcome.evidence.head_sha != state.binding.head_sha:
                return _reject(state, event, RejectionCode.INVALID_NO_FINDINGS_BINDING)
            return _apply(
                CompletedState(
                    run_id=state.run_id,
                    origin=state.origin,
                    limits=state.limits,
                    binding=state.binding,
                    cycle_number=state.cycle_number,
                    evidence=outcome.evidence,
                    completed_at=event.occurred_at,
                )
            )
        if isinstance(outcome, EligibleThreadsObservedOutcome):
            if outcome.frozen.head_sha != state.binding.head_sha:
                return _reject(state, event, RejectionCode.STALE_HEAD_SHA)
            if state.trigger_evidence is None:
                return _reject(state, event, RejectionCode.MISSING_TRIGGER_EVIDENCE)
            if outcome.frozen.trigger_marker != state.trigger_evidence.marker:
                return _reject(
                    state,
                    event,
                    RejectionCode.MISMATCHED_OUTCOME_BINDING,
                    effect_id=effect.effect_id,
                    detail="frozen trigger_marker must match confirmed trigger",
                )
            if outcome.frozen.cycle_number != state.cycle_number:
                return _reject(state, event, RejectionCode.STALE_CYCLE)
            if effect.trigger_marker != state.trigger_evidence.marker:
                return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
            adj_id, adj_idem = stable_effect_ids(
                run_id=state.run_id,
                cycle_number=state.cycle_number,
                operation="adjudicate_threads",
            )
            adjudicate = AdjudicateThreadsEffect(
                effect_id=adj_id,
                idempotency_key=adj_idem,
                run_id=state.run_id,
                cycle_number=state.cycle_number,
                attempt=1,
                max_attempts=max_attempts,
                repository=state.binding.repository,
                bound_head_sha=state.binding.head_sha,
                binding=state.binding,
                frozen_thread_ids=outcome.frozen.thread_ids,
                snapshot_ref=outcome.frozen.snapshot_ref,
                execution_context_ref=_execution_context_ref(state),
            )
            adjudicating = AdjudicatingState(
                run_id=state.run_id,
                origin=state.origin,
                limits=state.limits,
                binding=state.binding,
                cycle_number=state.cycle_number,
                entered_at=event.occurred_at,
                frozen=outcome.frozen,
                trigger_evidence=state.trigger_evidence,
                active_effect=adjudicate,
            )
            return _apply(adjudicating, adjudicate)
    return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)


def _reply_intents_from_evidence(evidence: AdjudicationEvidence) -> tuple[ReplyIntent, ...]:
    intents: list[ReplyIntent] = []
    for decision in evidence.decisions:
        if decision.decision in {
            AdjudicationDecisionKind.NOT_APPLICABLE,
            AdjudicationDecisionKind.UNCERTAIN,
        }:
            if decision.reply_ref is None:
                return ()
            intents.append(ReplyIntent(thread_id=decision.thread_id, reply_ref=decision.reply_ref))
    return tuple(intents)


def _handle_success_adjudicating(
    state: AdjudicatingState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    if effect.kind != "adjudicate_threads" or not isinstance(
        event.outcome, AdjudicationRecordedOutcome
    ):
        return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
    evidence = event.outcome.evidence
    if (
        evidence.frozen.thread_ids != state.frozen.thread_ids
        or evidence.frozen.snapshot_ref != state.frozen.snapshot_ref
        or evidence.frozen.head_sha != state.frozen.head_sha
        or evidence.frozen.cycle_number != state.frozen.cycle_number
        or evidence.frozen.trigger_marker != state.frozen.trigger_marker
    ):
        return _reject(
            state,
            event,
            RejectionCode.MISMATCHED_OUTCOME_BINDING,
            effect_id=effect.effect_id,
            detail="adjudication frozen provenance must match state.frozen exactly",
        )
    if set(evidence.frozen.thread_ids) != set(state.frozen.thread_ids):
        return _reject(state, event, RejectionCode.INVALID_THREAD_COVERAGE)
    max_attempts = state.limits.github_max_attempts_per_batch
    all_actionable = all(
        item.decision is AdjudicationDecisionKind.ACTIONABLE for item in evidence.decisions
    )
    if all_actionable:
        if evidence.fix_prompt_ref is None:
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_BINDING)
        thread_ids = tuple(item.thread_id for item in evidence.decisions)
        fix_id, fix_idem = stable_effect_ids(
            run_id=state.run_id, cycle_number=state.cycle_number, operation="run_local_fix"
        )
        local = RunLocalFixEffect(
            effect_id=fix_id,
            idempotency_key=fix_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=state.binding.repository,
            bound_head_sha=state.binding.head_sha,
            binding=state.binding,
            actionable_thread_ids=thread_ids,
            fix_prompt_ref=evidence.fix_prompt_ref,
            execution_context_ref=_execution_context_ref(state),
        )
        running = RunningLocalFixState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            binding=state.binding,
            cycle_number=state.cycle_number,
            entered_at=event.occurred_at,
            actionable_thread_ids=thread_ids,
            fix_prompt_ref=evidence.fix_prompt_ref,
            active_effect=local,
            trigger_evidence=state.trigger_evidence,
            adjudication=evidence,
        )
        return _apply(running, local)
    replies = _reply_intents_from_evidence(evidence)
    if not replies:
        return _reject(state, event, RejectionCode.INVALID_THREAD_COVERAGE)
    head = replies[0]
    reply_id, reply_idem = stable_effect_ids(
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        operation="post_thread_reply",
        target=head.thread_id,
    )
    reply = PostThreadReplyEffect(
        effect_id=reply_id,
        idempotency_key=reply_idem,
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        attempt=1,
        max_attempts=max_attempts,
        repository=state.binding.repository,
        bound_head_sha=state.binding.head_sha,
        binding=state.binding,
        thread_id=head.thread_id,
        reply_ref=head.reply_ref,
    )
    waiting_user = WaitingForUserState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=state.binding,
        cycle_number=state.cycle_number,
        entered_at=event.occurred_at,
        adjudication=evidence,
        remaining_replies=replies,
        active_effect=reply,
        safe_action=SafeAction(
            kind=SafeActionKind.CONTINUE_AFTER_USER_REPLY,
            condition="post remaining replies then continue observation",
        ),
        trigger_evidence=state.trigger_evidence,
    )
    return _apply(waiting_user, reply)


def _handle_success_waiting_for_user(
    state: WaitingForUserState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    if effect.kind != "post_thread_reply" or not isinstance(
        event.outcome, ThreadReplyConfirmedOutcome
    ):
        return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
    if event.outcome.thread_id != effect.thread_id:
        return _reject(state, event, RejectionCode.WRONG_THREAD_ID)
    if not isinstance(effect, PostThreadReplyEffect):
        return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
    if event.outcome.reply_ref != effect.reply_ref:
        return _reject(
            state,
            event,
            RejectionCode.MISMATCHED_OUTCOME_BINDING,
            effect_id=effect.effect_id,
            detail="reply_ref must match the active post_thread_reply effect",
        )
    remaining = state.remaining_replies[1:]
    if not remaining:
        new_state = state.model_copy(
            update={
                "remaining_replies": (),
                "active_effect": None,
                "safe_action": SafeAction(
                    kind=SafeActionKind.CONTINUE_AFTER_USER_REPLY,
                    condition="operator confirms continuation after replies",
                ),
            }
        )
        return _apply(new_state)
    head = remaining[0]
    reply_id, reply_idem = stable_effect_ids(
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        operation="post_thread_reply",
        target=head.thread_id,
    )
    reply = PostThreadReplyEffect(
        effect_id=reply_id,
        idempotency_key=reply_idem,
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        attempt=1,
        max_attempts=state.limits.github_max_attempts_per_batch,
        repository=state.binding.repository,
        bound_head_sha=state.binding.head_sha,
        binding=state.binding,
        thread_id=head.thread_id,
        reply_ref=head.reply_ref,
    )
    new_state = state.model_copy(update={"remaining_replies": remaining, "active_effect": reply})
    return _apply(new_state, reply)


def _handle_success_local_fix(
    state: RunningLocalFixState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    if effect.kind != "run_local_fix" or not isinstance(event.outcome, LocalFixFinishedOutcome):
        return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
    outcome = event.outcome
    if outcome.outcome is LocalFixOutcomeKind.ABORTED:
        return _apply(
            AbortedState(
                run_id=state.run_id,
                origin=state.origin,
                limits=state.limits,
                cycle_number=state.cycle_number,
                reason="local_fix_aborted",
                aborted_at=event.occurred_at,
                binding=state.binding,
            )
        )
    if outcome.outcome in {
        LocalFixOutcomeKind.MAX_ITERATIONS_REACHED,
        LocalFixOutcomeKind.PAUSED,
        LocalFixOutcomeKind.FAILED,
    }:
        reason = outcome.pause_reason or PauseReasonKind.LOCAL_FIX_PAUSED
        if outcome.outcome is LocalFixOutcomeKind.MAX_ITERATIONS_REACHED:
            reason = PauseReasonKind.LOCAL_FIX_LIMIT_REACHED
        elif outcome.outcome is LocalFixOutcomeKind.FAILED:
            reason = PauseReasonKind.LOCAL_FIX_FAILED
        if outcome.safe_action is None:
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_BINDING)
        return _apply(
            PausedState(
                run_id=state.run_id,
                origin=state.origin,
                limits=state.limits,
                cycle_number=state.cycle_number,
                reason=reason,
                safe_action=outcome.safe_action,
                safe_summary=f"local fix ended with {outcome.outcome.value}",
                paused_at=event.occurred_at,
                binding=state.binding,
                resumable=None,
            )
        )
    if outcome.accepted_patch_ref is None or outcome.new_head_sha is None:
        return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_BINDING)
    gen = _make_generate_publication_text(
        run_id=state.run_id,
        cycle=state.cycle_number,
        repository=state.binding.repository,
        head_sha=outcome.new_head_sha,
        evidence_ref=outcome.result_ref,
        patch_ref=outcome.accepted_patch_ref,
        max_attempts=state.limits.github_max_attempts_per_batch,
    )
    new_state = PublishingFixState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=state.binding,
        cycle_number=state.cycle_number,
        entered_at=event.occurred_at,
        old_head_sha=state.binding.head_sha,
        new_head_sha=outcome.new_head_sha,
        accepted_patch_ref=outcome.accepted_patch_ref,
        corrected_thread_ids=state.actionable_thread_ids,
        remaining_resolutions=state.actionable_thread_ids,
        step=PublicationStep.GENERATE_PUBLICATION_TEXT,
        active_effect=gen,
        trigger_evidence=state.trigger_evidence,
    )
    return _apply(new_state, gen)


def _advance_after_fix_publication(
    state: PublishingFixState, event: EffectSucceeded
) -> TransitionResult:
    if state.cycle_number >= state.limits.max_external_cycles:
        return _apply(
            PausedState(
                run_id=state.run_id,
                origin=state.origin,
                limits=state.limits,
                cycle_number=state.cycle_number,
                reason=PauseReasonKind.EXTERNAL_CYCLE_LIMIT_REACHED,
                safe_action=SafeAction(
                    kind=SafeActionKind.OPEN_NEW_CYCLE_OR_STOP,
                    condition="external cycle limit reached after fix publication",
                ),
                safe_summary="external cycle limit reached",
                paused_at=event.occurred_at,
                binding=state.binding.model_copy(update={"head_sha": state.new_head_sha})
                if state.new_head_sha
                else state.binding,
                resumable=None,
            )
        )
    if state.new_head_sha is None:
        return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
    next_cycle = state.cycle_number + 1
    binding = state.binding.model_copy(update={"head_sha": state.new_head_sha})
    request = _make_request_bot_review(
        run_id=state.run_id,
        cycle=next_cycle,
        binding=binding,
        max_attempts=state.limits.github_max_attempts_per_batch,
    )
    new_state = WaitingForBotState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=binding,
        cycle_number=next_cycle,
        entered_at=event.occurred_at,
        poll_sequence=1,
        active_effect=request,
        trigger_evidence=None,
    )
    return _apply(new_state, request)


def _handle_success_publishing_fix(
    state: PublishingFixState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    outcome = event.outcome
    max_attempts = state.limits.github_max_attempts_per_batch
    if effect.kind == "generate_publication_text":
        if not isinstance(outcome, PublicationTextPreparedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if state.new_head_sha is None:
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        commit_id, commit_idem = stable_effect_ids(
            run_id=state.run_id, cycle_number=state.cycle_number, operation="commit_patch"
        )
        commit = CommitPatchEffect(
            effect_id=commit_id,
            idempotency_key=commit_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=state.binding.repository,
            bound_head_sha=state.old_head_sha,
            patch_ref=state.accepted_patch_ref,
            expected_head_sha=state.old_head_sha,
            commit_message_ref=outcome.commit_message_ref,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.COMMIT_PATCH,
                "active_effect": commit,
                "publication_text_ref": outcome.publication_text_ref,
                "commit_message_ref": outcome.commit_message_ref,
            }
        )
        return _apply(new_state, commit)
    if effect.kind == "commit_patch":
        if not isinstance(outcome, CommitRecordedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, CommitPatchEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if outcome.commit_sha != outcome.new_head_sha:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="commit_sha must equal new_head_sha",
            )
        if outcome.new_head_sha == effect.expected_head_sha:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="new_head_sha must advance past expected_head_sha",
            )
        push_id, push_idem = stable_effect_ids(
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            operation="push_commit",
            target=outcome.commit_sha,
        )
        push = PushCommitEffect(
            effect_id=push_id,
            idempotency_key=push_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=state.binding.repository,
            bound_head_sha=outcome.new_head_sha,
            commit_sha=outcome.commit_sha,
            remote_ref=state.binding.head_branch,
            force=False,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.PUSH_COMMIT,
                "active_effect": push,
                "commit_sha": outcome.commit_sha,
                "new_head_sha": outcome.new_head_sha,
            }
        )
        return _apply(new_state, push)
    if effect.kind == "push_commit":
        if not isinstance(outcome, PushConfirmedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, PushCommitEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if outcome.commit_sha != effect.commit_sha or outcome.remote_ref != effect.remote_ref:
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="push confirmation must match effect commit_sha and remote_ref",
            )
        if state.publication_text_ref is None or state.new_head_sha is None:
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        binding = state.binding.model_copy(update={"head_sha": state.new_head_sha})
        text_id, text_idem = stable_effect_ids(
            run_id=state.run_id, cycle_number=state.cycle_number, operation="update_pr_text"
        )
        update = UpdatePrTextEffect(
            effect_id=text_id,
            idempotency_key=text_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=binding.repository,
            bound_head_sha=binding.head_sha,
            binding=binding,
            publication_text_ref=state.publication_text_ref,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.UPDATE_PR_TEXT,
                "active_effect": update,
                "binding": binding,
            }
        )
        return _apply(new_state, update)
    if effect.kind == "update_pr_text":
        if not isinstance(outcome, PrTextUpdatedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if not isinstance(effect, UpdatePrTextEffect):
            return _reject(state, event, RejectionCode.INVARIANT_VIOLATION)
        if (
            outcome.binding != effect.binding
            or outcome.publication_text_ref != effect.publication_text_ref
        ):
            return _reject(
                state,
                event,
                RejectionCode.MISMATCHED_OUTCOME_BINDING,
                effect_id=effect.effect_id,
                detail="PR text update must match effect binding and publication_text_ref",
            )
        remaining = state.remaining_resolutions
        if not remaining:
            return _advance_after_fix_publication(
                state.model_copy(
                    update={
                        "step": PublicationStep.COMPLETE,
                        "active_effect": None,
                        "binding": outcome.binding,
                    }
                ),
                event,
            )
        head = remaining[0]
        resolve_id, resolve_idem = stable_effect_ids(
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            operation="resolve_thread",
            target=head,
        )
        resolve = ResolveThreadEffect(
            effect_id=resolve_id,
            idempotency_key=resolve_idem,
            run_id=state.run_id,
            cycle_number=state.cycle_number,
            attempt=1,
            max_attempts=max_attempts,
            repository=outcome.binding.repository,
            bound_head_sha=outcome.binding.head_sha,
            binding=outcome.binding,
            thread_id=head,
        )
        new_state = state.model_copy(
            update={
                "step": PublicationStep.RESOLVE_THREAD,
                "active_effect": resolve,
                "binding": outcome.binding,
                "remaining_resolutions": remaining,
            }
        )
        return _apply(new_state, resolve)
    if effect.kind == "resolve_thread":
        if not isinstance(outcome, ThreadResolutionConfirmedOutcome):
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
        if outcome.thread_id != effect.thread_id:
            return _reject(state, event, RejectionCode.WRONG_THREAD_ID)
        remaining = state.remaining_resolutions[1:]
        if remaining:
            head = remaining[0]
            resolve_id, resolve_idem = stable_effect_ids(
                run_id=state.run_id,
                cycle_number=state.cycle_number,
                operation="resolve_thread",
                target=head,
            )
            resolve = ResolveThreadEffect(
                effect_id=resolve_id,
                idempotency_key=resolve_idem,
                run_id=state.run_id,
                cycle_number=state.cycle_number,
                attempt=1,
                max_attempts=max_attempts,
                repository=state.binding.repository,
                bound_head_sha=state.binding.head_sha,
                binding=state.binding,
                thread_id=head,
            )
            new_state = state.model_copy(
                update={"active_effect": resolve, "remaining_resolutions": remaining}
            )
            return _apply(new_state, resolve)
        completed = state.model_copy(
            update={
                "step": PublicationStep.COMPLETE,
                "active_effect": None,
                "remaining_resolutions": (),
            }
        )
        return _advance_after_fix_publication(completed, event)
    return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)


def _apply_original_success(
    suspended: ResumableContinuation, event: EffectSucceeded, confirmed: EffectSuccessOutcome
) -> TransitionResult:
    original = active_effect(suspended)
    if original is None:
        return _reject(
            suspended,
            event,
            RejectionCode.INVARIANT_VIOLATION,
            detail="suspended operational state missing active effect",
        )
    synthetic = EffectSucceeded(
        occurred_at=event.occurred_at,
        token=event.token.model_copy(
            update={
                "effect_id": original.effect_id,
                "cycle_number": original.cycle_number,
                "bound_head_sha": original.bound_head_sha,
            }
        ),
        outcome=confirmed,
    )
    return _handle_effect_succeeded(suspended, synthetic)


def _handle_success_reconciling(
    state: ReconcilingWriteState, event: EffectSucceeded, effect: PrReviewEffect
) -> TransitionResult:
    if effect.kind != "reconcile_write" or not isinstance(
        event.outcome, ReconciliationResolvedOutcome
    ):
        return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_KIND)
    outcome = event.outcome
    if outcome.original_effect_id != state.original_write.effect_id:
        return _reject(state, event, RejectionCode.STALE_EFFECT_ID)
    if outcome.resolution is ReconciliationResolutionKind.APPLIED:
        if outcome.confirmed_outcome is None:
            return _reject(state, event, RejectionCode.MISMATCHED_OUTCOME_BINDING)
        return _apply_original_success(state.suspended, event, outcome.confirmed_outcome)
    if outcome.resolution is ReconciliationResolutionKind.PROVEN_NOT_APPLIED:
        if outcome.next_attempt_at is None:
            return _reject(state, event, RejectionCode.MISSING_RETRY_ELIGIBILITY)
        if outcome.next_attempt_at <= event.occurred_at:
            return _reject(
                state,
                event,
                RejectionCode.RETRY_TIME_NOT_IN_FUTURE,
                effect_id=effect.effect_id,
            )
        original = state.original_write
        if original.attempt >= original.max_attempts:
            return _to_paused_retry_exhausted(
                state.suspended,
                original,
                error=state.last_error,
                occurred_at=event.occurred_at,
            )
        # Retry the original write later; never re-emit it from reconciliation.
        return _to_waiting_retry(
            state.suspended,
            original,
            next_attempt=original.attempt + 1,
            next_attempt_at=outcome.next_attempt_at,
            error=state.last_error,
            occurred_at=event.occurred_at,
        )
    # unresolved
    return _apply(
        PausedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=state.cycle_number,
            reason=PauseReasonKind.AMBIGUOUS_WRITE_UNRESOLVED,
            safe_action=SafeAction(
                kind=SafeActionKind.RECONCILE_OR_INSPECT,
                condition="operator inspects ambiguous write before resume",
            ),
            safe_summary=state.last_error.safe_summary,
            paused_at=event.occurred_at,
            binding=state.binding,
            resumable=state,
        )
    )


def _handle_effect_succeeded(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, EffectSucceeded)
    if state.kind in {"prepared", "paused", "waiting_retry"} or state.kind in TERMINAL_STATE_KINDS:
        return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)
    validated = _validate_token(state, event, event)
    if isinstance(validated, TransitionRejected):
        return validated
    effect = validated
    allowed = OUTCOME_FOR_EFFECT.get(effect.kind, frozenset())
    if event.outcome.kind not in allowed:
        return _reject(
            state,
            event,
            RejectionCode.MISMATCHED_OUTCOME_KIND,
            effect_id=effect.effect_id,
            detail=f"{effect.kind}->{event.outcome.kind}",
        )
    if isinstance(state, PublishingInitialState):
        return _handle_success_publishing_initial(state, event, effect)
    if isinstance(state, WaitingForBotState):
        return _handle_success_waiting_for_bot(state, event, effect)
    if isinstance(state, AdjudicatingState):
        return _handle_success_adjudicating(state, event, effect)
    if isinstance(state, WaitingForUserState):
        return _handle_success_waiting_for_user(state, event, effect)
    if isinstance(state, RunningLocalFixState):
        return _handle_success_local_fix(state, event, effect)
    if isinstance(state, PublishingFixState):
        return _handle_success_publishing_fix(state, event, effect)
    if isinstance(state, ReconcilingWriteState):
        return _handle_success_reconciling(state, event, effect)
    return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)


def _handle_abort(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, AbortRequested)
    if state.kind in TERMINAL_STATE_KINDS:
        return _reject(state, event, RejectionCode.TERMINAL_STATE)
    return _abort(state, event)


def _handle_fatal(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    assert isinstance(event, FatalFailureDetected)
    if state.kind in TERMINAL_STATE_KINDS:
        return _reject(state, event, RejectionCode.TERMINAL_STATE)
    return _fail(state, event)


def _unsupported(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    if state.kind in TERMINAL_STATE_KINDS:
        return _reject(state, event, RejectionCode.TERMINAL_STATE)
    return _reject(state, event, RejectionCode.UNSUPPORTED_TRANSITION)


def _build_registry() -> dict[tuple[str, str], Handler]:
    registry: dict[tuple[str, str], Handler] = {}
    for state_kind in STATE_KINDS:
        for event_kind in EVENT_KINDS:
            registry[(state_kind, event_kind)] = _unsupported

    registry[("prepared", "start_requested")] = _handle_start
    for sk in (
        "publishing_initial",
        "waiting_for_bot",
        "adjudicating",
        "waiting_for_user",
        "running_local_fix",
        "publishing_fix",
        "reconciling_write",
    ):
        registry[(sk, "effect_succeeded")] = _handle_effect_succeeded
        registry[(sk, "effect_retryable_failure")] = _handle_retryable_failure
        registry[(sk, "effect_blocked")] = _handle_blocked
        registry[(sk, "write_outcome_uncertain")] = _handle_uncertain

    registry[("waiting_retry", "retry_due")] = _handle_retry_due
    registry[("paused", "resume_requested")] = _handle_resume
    registry[("waiting_for_user", "user_continuation_requested")] = _handle_user_continuation

    for sk in STATE_KINDS:
        if sk not in TERMINAL_STATE_KINDS:
            registry[(sk, "abort_requested")] = _handle_abort
            registry[(sk, "fatal_failure_detected")] = _handle_fatal
    return registry


TRANSITION_REGISTRY: Final[Mapping[tuple[str, str], Handler]] = _build_registry()


def reduce_pr_review(state: PrReviewState, event: PrReviewEvent) -> TransitionResult:
    """Apply one event to state. Invalid transitions return typed rejection."""
    handler = TRANSITION_REGISTRY[(state.kind, event.kind)]
    return handler(state, event)


def accepted_transition_pairs() -> frozenset[tuple[str, str]]:
    """Pairs whose registered handler is not the generic unsupported rejector."""
    accepted: set[tuple[str, str]] = set()
    for key, handler in TRANSITION_REGISTRY.items():
        if handler is not _unsupported:
            accepted.add(key)
    return frozenset(accepted)

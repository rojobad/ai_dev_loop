"""Full workflow path, retry, reconciliation, and determinism tests."""

from __future__ import annotations

import json

from tests.unit.pr_review_v2.helpers import (
    SHA_B,
    T1,
    T2,
    T_LATER,
    T_RETRY,
    actionable_adjudication,
    artifact,
    confirm_trigger,
    drive_fix_publication,
    drive_initial_publication_to_waiting_for_bot,
    freeze_threads,
    publication_text_outcome,
    reply_adjudication,
    start,
    succeed,
    token_for,
)

from ai_dev_loop.pr_review_v2.domain import (
    PR_REVIEW_STATE_ADAPTER,
    AbortRequested,
    AdjudicationRecordedOutcome,
    BotStillWaitingOutcome,
    EffectRetryableFailure,
    EffectSucceeded,
    ErrorSummary,
    FailureReasonKind,
    FatalFailureDetected,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PauseReasonKind,
    ReconciliationResolutionKind,
    ReconciliationResolvedOutcome,
    RejectionCode,
    ResumeRequested,
    RetryDue,
    TransientErrorKind,
    TransitionApplied,
    TransitionRejected,
    UserContinuationEvidence,
    UserContinuationRequested,
    VerifiedNoFindingsEvidence,
    VerifiedNoFindingsOutcome,
    WriteOutcomeUncertain,
    reduce_pr_review,
)


def test_source_run_path_to_completed(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = succeed(
        state,
        effects[0],
        VerifiedNoFindingsOutcome(
            evidence=VerifiedNoFindingsEvidence(
                head_sha=binding.head_sha,
                observation_ref=artifact("obs.json"),
                verified_at=T2,
            )
        ),
    )
    assert state.kind == "completed"
    assert effects == ()


def test_existing_pr_path_and_normal_poll_does_not_consume_retry(
    prepared_existing,
) -> None:
    state, effects = start(prepared_existing)
    assert state.kind == "waiting_for_bot"
    assert effects[0].kind == "request_bot_review"
    assert effects[0].attempt == 1
    binding = state.binding
    state, effects = confirm_trigger(state, effects[0], binding)
    assert effects[0].kind == "observe_bot_review"
    assert effects[0].attempt == 1
    first_poll = effects[0].poll_sequence
    state, effects = succeed(
        state,
        effects[0],
        BotStillWaitingOutcome(next_not_before=T_LATER, poll_sequence=first_poll + 1),
    )
    assert state.kind == "waiting_for_bot"
    assert state.poll_sequence == first_poll + 1
    assert effects[0].attempt == 1
    assert effects[0].kind == "observe_bot_review"
    assert state.kind != "waiting_retry"


def test_user_attention_path_and_continuation(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("t1", "t2"))
    evidence = reply_adjudication(state.frozen)
    state, effects = succeed(state, effects[0], AdjudicationRecordedOutcome(evidence=evidence))
    assert state.kind == "waiting_for_user"
    assert effects[0].kind == "post_thread_reply"
    # Complete replies
    while effects:
        state, effects = succeed(
            state,
            effects[0],
            __import__(
                "ai_dev_loop.pr_review_v2.domain", fromlist=["ThreadReplyConfirmedOutcome"]
            ).ThreadReplyConfirmedOutcome(
                thread_id=effects[0].thread_id, reply_ref=effects[0].reply_ref
            ),
        )
        if state.active_effect is None:
            break
    assert state.active_effect is None
    early = reduce_pr_review(
        state.model_copy(
            update={
                "remaining_replies": state.remaining_replies,
                "active_effect": state.active_effect,
            }
        )
        if False
        else state,
        UserContinuationRequested(
            occurred_at=T2,
            evidence=UserContinuationEvidence(
                repository=binding.repository,
                pr_number=binding.pr_number,
                cycle_number=state.cycle_number,
                head_sha=binding.head_sha,
                evidence_ref=artifact("continue.json"),
            ),
        ),
    )
    # Build a mid-reply state and ensure continuation is rejected
    mid_state, mid_effects, mid_binding = drive_initial_publication_to_waiting_for_bot(
        prepared_source
    )
    mid_state, mid_effects = confirm_trigger(mid_state, mid_effects[0], mid_binding)
    mid_state, mid_effects = freeze_threads(mid_state, mid_effects[0], mid_binding, ("t1", "t2"))
    mid_state, mid_effects = succeed(
        mid_state,
        mid_effects[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(mid_state.frozen)),
    )
    rejected = reduce_pr_review(
        mid_state,
        UserContinuationRequested(
            occurred_at=T2,
            evidence=UserContinuationEvidence(
                repository=mid_binding.repository,
                pr_number=mid_binding.pr_number,
                cycle_number=mid_state.cycle_number,
                head_sha=mid_binding.head_sha,
                evidence_ref=artifact("continue.json"),
            ),
        ),
    )
    assert isinstance(rejected, TransitionRejected)
    assert rejected.code is RejectionCode.USER_CONTINUATION_BEFORE_REPLIES
    assert isinstance(early, TransitionApplied)
    assert early.state.kind == "waiting_for_bot"
    assert early.effects[0].kind == "observe_bot_review"


def test_local_fix_and_multi_cycle_then_cycle_limit(prepared_source) -> None:
    limits = prepared_source.limits.model_copy(update={"max_external_cycles": 1})
    prepared = prepared_source.model_copy(update={"limits": limits})
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("fix-1",))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(state.frozen)),
    )
    assert state.kind == "running_local_fix"
    state, effects = succeed(
        state,
        effects[0],
        LocalFixFinishedOutcome(
            outcome=LocalFixOutcomeKind.ACCEPTED,
            accepted_patch_ref=artifact("artifacts/fix.patch"),
            new_head_sha="f" * 40,
            result_ref=artifact("artifacts/local-result.json"),
        ),
    )
    assert state.kind == "publishing_fix"
    state, effects = drive_fix_publication(state, effects)
    assert state.kind == "paused"
    assert state.reason is PauseReasonKind.EXTERNAL_CYCLE_LIMIT_REACHED


def test_multi_cycle_without_limit(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("fix-1",))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(state.frozen)),
    )
    state, effects = succeed(
        state,
        effects[0],
        LocalFixFinishedOutcome(
            outcome=LocalFixOutcomeKind.ACCEPTED,
            accepted_patch_ref=artifact("artifacts/fix.patch"),
            new_head_sha="f" * 40,
            result_ref=artifact("artifacts/local-result.json"),
        ),
    )
    state, effects = drive_fix_publication(state, effects)
    assert state.kind == "waiting_for_bot"
    assert state.cycle_number == 2
    assert effects[0].kind == "request_bot_review"
    assert effects[0].bound_head_sha == "e" * 40


def test_retry_budget_six_attempts_then_resume_same_identity(prepared_source) -> None:
    state, effects = start(prepared_source)
    effect = effects[0]
    original_id = effect.effect_id
    original_key = effect.idempotency_key
    for attempt in range(1, 6):
        result = reduce_pr_review(
            state,
            EffectRetryableFailure(
                occurred_at=T2,
                token=token_for(effect),
                error=ErrorSummary(kind=TransientErrorKind.HTTP_429, safe_summary="rate"),
                failed_attempt=attempt,
                next_attempt_at=T_LATER,
            ),
        )
        assert isinstance(result, TransitionApplied)
        assert result.state.kind == "waiting_retry"
        assert result.effects == ()
        due = reduce_pr_review(
            result.state,
            RetryDue(
                occurred_at=T_LATER,
                pending_effect_id=result.state.retrying_effect_id,
                current_time=T_LATER,
            ),
        )
        assert isinstance(due, TransitionApplied)
        state = due.state
        effect = due.effects[0]
        assert effect.effect_id == original_id
        assert effect.idempotency_key == original_key
        assert effect.attempt == attempt + 1
    # attempt 6 fails -> paused
    result = reduce_pr_review(
        state,
        EffectRetryableFailure(
            occurred_at=T2,
            token=token_for(effect),
            error=ErrorSummary(kind=TransientErrorKind.HTTP_429, safe_summary="rate"),
            failed_attempt=6,
            next_attempt_at=T_LATER,
        ),
    )
    assert isinstance(result, TransitionApplied)
    assert result.state.kind == "paused"
    assert result.state.reason is PauseReasonKind.RETRY_EXHAUSTED
    resumed = reduce_pr_review(result.state, ResumeRequested(occurred_at=T2))
    assert isinstance(resumed, TransitionApplied)
    assert resumed.effects[0].effect_id == original_id
    assert resumed.effects[0].idempotency_key == original_key
    assert resumed.effects[0].attempt == 1


def test_early_retry_timer_rejected(prepared_source) -> None:
    state, effects = start(prepared_source)
    waiting = reduce_pr_review(
        state,
        EffectRetryableFailure(
            occurred_at=T2,
            token=token_for(effects[0]),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="t"),
            failed_attempt=1,
            next_attempt_at=T_LATER,
        ),
    )
    assert isinstance(waiting, TransitionApplied)
    early = reduce_pr_review(
        waiting.state,
        RetryDue(
            occurred_at=T1,
            pending_effect_id=waiting.state.retrying_effect_id,
            current_time=T1,
        ),
    )
    assert isinstance(early, TransitionRejected)
    assert early.code is RejectionCode.EARLY_RETRY_TIMER


def test_ambiguous_write_reconciliation_paths(prepared_source) -> None:
    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    commit = effects[0]
    uncertain = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(commit),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-1",
            original_write=commit,
        ),
    )
    assert isinstance(uncertain, TransitionApplied)
    assert uncertain.state.kind == "reconciling_write"
    assert uncertain.effects[0].kind == "reconcile_write"
    assert all(effect.kind != "commit_patch" for effect in uncertain.effects)

    # applied -> continues as commit success without repeating commit
    applied = reduce_pr_review(
        uncertain.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(uncertain.effects[0]),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.APPLIED,
                original_effect_id=commit.effect_id,
                confirmed_outcome=__import__(
                    "ai_dev_loop.pr_review_v2.domain", fromlist=["CommitRecordedOutcome"]
                ).CommitRecordedOutcome(
                    commit_sha=SHA_B, new_head_sha=SHA_B, expected_remote_sha_before_push=None
                ),
            ),
        ),
    )
    assert isinstance(applied, TransitionApplied)
    assert applied.state.kind == "publishing_initial"
    assert applied.effects[0].kind == "push_commit"

    # proven_not_applied -> waiting_retry for original write
    uncertain2 = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(commit),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-2",
            original_write=commit,
        ),
    )
    assert isinstance(uncertain2, TransitionApplied)
    not_applied = reduce_pr_review(
        uncertain2.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(uncertain2.effects[0]),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.PROVEN_NOT_APPLIED,
                original_effect_id=commit.effect_id,
                next_attempt_at=T_RETRY,
            ),
        ),
    )
    assert isinstance(not_applied, TransitionApplied)
    assert not_applied.state.kind == "waiting_retry"
    assert not_applied.state.retrying_effect_id == commit.effect_id
    assert (
        not_applied.state.next_attempt_at.isoformat()
        .replace("+00:00", "Z")
        .startswith("2026-07-20T12:00:30")
    )
    assert not_applied.effects == ()
    assert all(effect.kind != "commit_patch" for effect in not_applied.effects)

    # unresolved -> paused
    uncertain3 = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(commit),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-3",
            original_write=commit,
        ),
    )
    assert isinstance(uncertain3, TransitionApplied)
    unresolved = reduce_pr_review(
        uncertain3.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(uncertain3.effects[0]),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.UNRESOLVED,
                original_effect_id=commit.effect_id,
            ),
        ),
    )
    assert isinstance(unresolved, TransitionApplied)
    assert unresolved.state.kind == "paused"
    assert unresolved.state.reason is PauseReasonKind.AMBIGUOUS_WRITE_UNRESOLVED


def test_write_uncertain_rejected_for_read_only(prepared_existing) -> None:
    state, effects = start(prepared_existing)
    state, effects = confirm_trigger(state, effects[0], state.binding)
    observe = effects[0]
    result = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(observe),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="a"),
            reconciliation_identity="rec",
            original_write=__import__(
                "ai_dev_loop.pr_review_v2.domain.effects", fromlist=["CommitPatchEffect"]
            ).CommitPatchEffect(
                effect_id=observe.effect_id,
                idempotency_key=observe.effect_id,
                run_id=state.run_id,
                cycle_number=1,
                attempt=1,
                max_attempts=6,
                repository=state.binding.repository,
                bound_head_sha=state.binding.head_sha,
                patch_ref=artifact("p.patch"),
                expected_head_sha=state.binding.head_sha,
                expected_branch=state.binding.head_branch,
                commit_message_ref=artifact("m.txt"),
            ),
        ),
    )
    assert isinstance(result, TransitionRejected)
    assert result.code is RejectionCode.WRITE_UNCERTAIN_FOR_NON_MUTATING


def test_fatal_and_determinism(prepared_source) -> None:
    state, effects = start(prepared_source)
    event = FatalFailureDetected(
        occurred_at=T2,
        reason=FailureReasonKind.HASH_MISMATCH,
        safe_summary="hash mismatch",
    )
    first = reduce_pr_review(state, event)
    second = reduce_pr_review(
        __import__(
            "ai_dev_loop.pr_review_v2.domain", fromlist=["parse_pr_review_state"]
        ).parse_pr_review_state(json.loads(PR_REVIEW_STATE_ADAPTER.dump_json(state))),
        event,
    )
    assert isinstance(first, TransitionApplied)
    assert isinstance(second, TransitionApplied)
    assert first.state.kind == "failed"
    assert PR_REVIEW_STATE_ADAPTER.dump_json(first.state) == PR_REVIEW_STATE_ADAPTER.dump_json(
        second.state
    )
    assert first.effects == ()


def test_late_result_after_abort_rejected(prepared_source) -> None:
    state, effects = start(prepared_source)
    aborted = reduce_pr_review(state, AbortRequested(occurred_at=T2))
    assert isinstance(aborted, TransitionApplied)
    late = reduce_pr_review(
        aborted.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(effects[0]),
            outcome=publication_text_outcome(),
        ),
    )
    assert isinstance(late, TransitionRejected)
    assert late.code is RejectionCode.TERMINAL_STATE

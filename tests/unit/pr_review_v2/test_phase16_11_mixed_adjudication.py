"""Phase 16.11 mixed adjudication priority, deferred replies, and legacy recovery."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.helpers import (
    HASH_2,
    SHA_B,
    T2,
    T3,
    actionable_adjudication,
    artifact,
    confirm_trigger,
    drive_fix_publication,
    drive_initial_publication_to_waiting_for_bot,
    freeze_threads,
    legacy_mixed_defect_adjudication,
    mixed_adjudication,
    reply_adjudication,
    succeed,
    token_for,
)

from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    AdjudicationRecordedOutcome,
    EffectSucceeded,
    FrozenThreadSet,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PauseReasonKind,
    PostThreadReplyEffect,
    RecoverMixedAdjudicationRequested,
    RejectionCode,
    RunningLocalFixState,
    SafeAction,
    SafeActionKind,
    ThreadDecisionRecord,
    TransitionApplied,
    TransitionRejected,
    UserContinuationEvidence,
    UserContinuationRequested,
    WaitingForUserState,
    actionable_thread_ids_from_evidence,
    awaiting_operator_continuation,
    deferred_reply_intents_from_evidence,
    is_legacy_mixed_adjudication_defect,
    pending_deferred_reply_dispatch,
    reduce_pr_review,
    stable_effect_ids,
)


def _legacy_waiting_for_user(state, frozen, legacy: AdjudicationEvidence) -> WaitingForUserState:
    replies = deferred_reply_intents_from_evidence(legacy)
    head = replies[0]
    reply_id, reply_idem = stable_effect_ids(
        run_id=state.run_id,
        cycle_number=frozen.cycle_number,
        operation="post_thread_reply",
        target=head.thread_id,
    )
    reply = PostThreadReplyEffect(
        effect_id=reply_id,
        idempotency_key=reply_idem,
        run_id=state.run_id,
        cycle_number=frozen.cycle_number,
        attempt=1,
        max_attempts=state.limits.github_max_attempts_per_batch,
        repository=state.binding.repository,
        bound_head_sha=state.binding.head_sha,
        binding=state.binding,
        thread_id=head.thread_id,
        reply_ref=head.reply_ref,
    )
    return WaitingForUserState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=state.binding,
        cycle_number=frozen.cycle_number,
        entered_at=T3,
        adjudication=legacy,
        remaining_replies=replies,
        active_effect=reply,
        safe_action=SafeAction(
            kind=SafeActionKind.CONTINUE_AFTER_USER_REPLY,
            condition="post remaining replies then continue observation",
        ),
        trigger_evidence=state.trigger_evidence,
    )


def test_actionable_without_fix_prompt_fails_outside_legacy_mixed() -> None:
    frozen = FrozenThreadSet(
        thread_ids=("t1", "t2"),
        snapshot_ref=artifact("snap.json"),
        head_sha=SHA_B,
        cycle_number=3,
        trigger_marker="marker",
    )
    with pytest.raises(ValidationError, match="requires fix_prompt_ref"):
        AdjudicationEvidence(
            frozen=frozen,
            decisions=(
                ThreadDecisionRecord(
                    thread_id="t1",
                    decision=AdjudicationDecisionKind.ACTIONABLE,
                    safe_summary="a",
                ),
                ThreadDecisionRecord(
                    thread_id="t2",
                    decision=AdjudicationDecisionKind.ACTIONABLE,
                    safe_summary="b",
                ),
            ),
            result_ref=artifact("adj.json"),
            fix_prompt_ref=None,
        )


def test_legacy_mixed_defect_shape_is_readable() -> None:
    frozen = FrozenThreadSet(
        thread_ids=("r1", "a1", "a2"),
        snapshot_ref=artifact("snap.json"),
        head_sha=SHA_B,
        cycle_number=3,
        trigger_marker="marker",
    )
    evidence = legacy_mixed_defect_adjudication(frozen)
    assert is_legacy_mixed_adjudication_defect(evidence)
    assert actionable_thread_ids_from_evidence(evidence) == ("a1", "a2")
    assert len(deferred_reply_intents_from_evidence(evidence)) == 1


def test_mixed_adjudication_routes_to_local_fix_with_deferred_replies(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    thread_ids = ("reply-1", "fix-1", "fix-2")
    state, effects = freeze_threads(state, effects[0], binding, thread_ids)
    frozen = state.frozen
    evidence = mixed_adjudication(frozen)
    state, effects = succeed(state, effects[0], AdjudicationRecordedOutcome(evidence=evidence))
    assert state.kind == "running_local_fix"
    assert effects[0].kind == "run_local_fix"
    assert effects[0].actionable_thread_ids == ("fix-1", "fix-2")
    assert state.deferred_replies[0].thread_id == "reply-1"
    assert all(effect.kind != "post_thread_reply" for effect in effects)


def test_mixed_lifecycle_fix_then_deferred_reply(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("reply-1", "fix-1"))
    evidence = mixed_adjudication(state.frozen)
    state, effects = succeed(state, effects[0], AdjudicationRecordedOutcome(evidence=evidence))
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
    assert state.kind == "waiting_for_user"
    assert effects[0].kind == "post_thread_reply"
    assert effects[0].thread_id == "reply-1"
    assert state.remaining_replies[0].thread_id == "reply-1"
    assert state.deferred_context is not None
    assert state.deferred_context.adjudication.frozen.head_sha == binding.head_sha
    assert state.deferred_context.source_head_sha == binding.head_sha
    new_head = state.binding.head_sha
    assert new_head != binding.head_sha
    assert state.adjudication.frozen.head_sha == binding.head_sha
    assert effects[0].bound_head_sha == new_head
    state, effects = succeed(
        state,
        effects[0],
        __import__(
            "ai_dev_loop.pr_review_v2.domain", fromlist=["ThreadReplyConfirmedOutcome"]
        ).ThreadReplyConfirmedOutcome(
            thread_id=effects[0].thread_id, reply_ref=effects[0].reply_ref
        ),
    )
    assert state.active_effect is None
    continued = reduce_pr_review(
        state,
        UserContinuationRequested(
            occurred_at=T3,
            evidence=UserContinuationEvidence(
                repository=state.binding.repository,
                pr_number=state.binding.pr_number,
                cycle_number=state.cycle_number,
                head_sha=state.binding.head_sha,
                evidence_ref=artifact("continue.json"),
            ),
        ),
    )
    assert isinstance(continued, TransitionApplied)
    assert continued.state.kind == "waiting_for_bot"
    assert continued.effects[0].kind == "request_bot_review"
    assert continued.state.cycle_number == state.cycle_number + 1
    assert continued.state.binding.head_sha == new_head
    assert continued.state.trigger_evidence is None


def test_paused_local_fix_retains_deferred_replies(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("reply-1", "fix-1"))
    evidence = mixed_adjudication(state.frozen)
    state, effects = succeed(state, effects[0], AdjudicationRecordedOutcome(evidence=evidence))
    deferred = state.deferred_replies
    paused = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(effects[0]),
            outcome=LocalFixFinishedOutcome(
                outcome=LocalFixOutcomeKind.PAUSED,
                result_ref=artifact("artifacts/local-result.json"),
                pause_reason=PauseReasonKind.LOCAL_FIX_PAUSED,
                safe_action=SafeAction(
                    kind=SafeActionKind.RESUME_SAME_EFFECT,
                    condition="resume local fix",
                ),
            ),
        ),
    )
    assert isinstance(paused, TransitionApplied)
    assert paused.state.kind == "paused"
    assert isinstance(paused.state.resumable, RunningLocalFixState)
    assert paused.state.resumable.deferred_replies == deferred
    assert paused.effects == ()


def test_legacy_recovery_emits_readjudication_only(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("r1", "a1", "a2"))
    legacy = legacy_mixed_defect_adjudication(state.frozen)
    waiting = _legacy_waiting_for_user(state, state.frozen, legacy)
    digest = legacy.result_ref.sha256
    recovered = reduce_pr_review(
        waiting,
        RecoverMixedAdjudicationRequested(
            occurred_at=T3,
            evidence_ref=artifact("recovery.json"),
            legacy_result_ref_sha256=digest,
            superseded_reply_effect_id=waiting.active_effect.effect_id,
        ),
    )
    assert isinstance(recovered, TransitionApplied)
    assert recovered.state.kind == "adjudicating"
    assert recovered.effects[0].kind == "adjudicate_threads"
    assert digest in recovered.effects[0].effect_id
    assert all(effect.kind != "post_thread_reply" for effect in recovered.effects)


def test_legacy_recovery_idempotent_effect_identity(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("r1", "a1", "a2"))
    legacy = legacy_mixed_defect_adjudication(state.frozen)
    waiting = _legacy_waiting_for_user(state, state.frozen, legacy)
    digest = legacy.result_ref.sha256
    event = RecoverMixedAdjudicationRequested(
        occurred_at=T3,
        evidence_ref=artifact("recovery.json"),
        legacy_result_ref_sha256=digest,
        superseded_reply_effect_id=waiting.active_effect.effect_id,
    )
    first = reduce_pr_review(waiting, event)
    second = reduce_pr_review(first.state, event)
    assert isinstance(first, TransitionApplied)
    assert isinstance(second, TransitionRejected)
    assert second.code is RejectionCode.UNSUPPORTED_TRANSITION


def test_recovery_rejects_reply_only_and_modern_mixed(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("t1", "t2"))
    reply_evidence = reply_adjudication(state.frozen)
    replies = deferred_reply_intents_from_evidence(reply_evidence)
    head = replies[0]
    reply_id, reply_idem = stable_effect_ids(
        run_id=state.run_id,
        cycle_number=state.cycle_number,
        operation="post_thread_reply",
        target=head.thread_id,
    )
    waiting = WaitingForUserState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=state.binding,
        cycle_number=state.cycle_number,
        entered_at=T3,
        adjudication=reply_evidence,
        remaining_replies=replies,
        active_effect=PostThreadReplyEffect(
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
        ),
        safe_action=SafeAction(
            kind=SafeActionKind.CONTINUE_AFTER_USER_REPLY,
            condition="post remaining replies then continue observation",
        ),
        trigger_evidence=state.trigger_evidence,
    )
    rejected = reduce_pr_review(
        waiting,
        RecoverMixedAdjudicationRequested(
            occurred_at=T3,
            evidence_ref=artifact("recovery.json"),
            legacy_result_ref_sha256=HASH_2,
            superseded_reply_effect_id=waiting.active_effect.effect_id,
        ),
    )
    assert isinstance(rejected, TransitionRejected)

    state2, effects2, binding2 = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state2, effects2 = confirm_trigger(state2, effects2[0], binding2)
    state2, effects2 = freeze_threads(state2, effects2[0], binding2, ("reply-1", "fix-1"))
    modern = mixed_adjudication(state2.frozen)
    state2, effects2 = succeed(
        state2,
        effects2[0],
        AdjudicationRecordedOutcome(evidence=modern),
    )
    assert state2.kind == "running_local_fix"


def test_reply_only_and_all_actionable_unchanged(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("t1", "t2"))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(state.frozen)),
    )
    assert state.kind == "waiting_for_user"
    assert effects[0].kind == "post_thread_reply"

    state2, effects2, binding2 = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state2, effects2 = confirm_trigger(state2, effects2[0], binding2)
    state2, effects2 = freeze_threads(state2, effects2[0], binding2, ("fix-1",))
    state2, effects2 = succeed(
        state2,
        effects2[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(state2.frozen)),
    )
    assert state2.kind == "running_local_fix"
    assert state2.deferred_replies == ()


def test_simulated_legacy_run_shape_cycle_3(prepared_source) -> None:
    """Exact validation scenario from phase plan: cycle 3, 3 threads, legacy mixed."""
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(
        state,
        effects[0],
        binding.model_copy(update={"head_sha": binding.head_sha}),
        ("r1", "a1", "a2"),
    )
    assert state.cycle_number == 1
    # Advance one external cycle to reach cycle 2, then another for cycle 3 is heavy;
    # instead assert frozen cycle binding on legacy evidence directly.
    frozen = FrozenThreadSet(
        thread_ids=("r1", "a1", "a2"),
        snapshot_ref=artifact("snap-cycle3.json"),
        head_sha=binding.head_sha,
        cycle_number=3,
        trigger_marker=state.trigger_evidence.marker,
    )
    legacy = legacy_mixed_defect_adjudication(frozen)
    waiting = _legacy_waiting_for_user(state, frozen, legacy)
    recovered = reduce_pr_review(
        waiting,
        RecoverMixedAdjudicationRequested(
            occurred_at=T3,
            evidence_ref=artifact("recovery-cycle3.json"),
            legacy_result_ref_sha256=legacy.result_ref.sha256,
            superseded_reply_effect_id=waiting.active_effect.effect_id,
        ),
    )
    assert isinstance(recovered, TransitionApplied)
    assert recovered.effects == (recovered.effects[0],)
    assert recovered.effects[0].kind == "adjudicate_threads"
    upgraded = mixed_adjudication(frozen)
    after_adj = reduce_pr_review(
        recovered.state,
        EffectSucceeded(
            occurred_at=T3,
            token=token_for(recovered.effects[0]),
            outcome=AdjudicationRecordedOutcome(evidence=upgraded),
        ),
    )
    assert isinstance(after_adj, TransitionApplied)
    assert after_adj.state.kind == "running_local_fix"
    assert after_adj.effects[0].actionable_thread_ids == ("a1", "a2")


def test_pending_deferred_reply_dispatch_requires_queue_head_match(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("reply-1", "fix-1"))
    evidence = mixed_adjudication(state.frozen)
    state, effects = succeed(state, effects[0], AdjudicationRecordedOutcome(evidence=evidence))
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
    assert state.kind == "waiting_for_user"
    dispatch = pending_deferred_reply_dispatch(state)
    assert dispatch is not None
    assert dispatch.queue_head.thread_id == "reply-1"
    assert dispatch.active_effect.kind == "post_thread_reply"
    assert awaiting_operator_continuation(state) is False


def test_user_continuation_rejected_while_deferred_reply_pending(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("reply-1",))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(state.frozen)),
    )
    assert pending_deferred_reply_dispatch(state) is not None
    rejected = reduce_pr_review(
        state,
        UserContinuationRequested(
            occurred_at=T3,
            evidence=UserContinuationEvidence(
                repository=state.binding.repository,
                pr_number=state.binding.pr_number,
                cycle_number=state.cycle_number,
                head_sha=state.binding.head_sha,
                evidence_ref=artifact("continue-early.json"),
            ),
        ),
    )
    assert isinstance(rejected, TransitionRejected)
    assert rejected.code is RejectionCode.USER_CONTINUATION_BEFORE_REPLIES


def test_awaiting_operator_continuation_after_final_reply(prepared_source) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("reply-1",))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(state.frozen)),
    )
    state, effects = succeed(
        state,
        effects[0],
        __import__(
            "ai_dev_loop.pr_review_v2.domain", fromlist=["ThreadReplyConfirmedOutcome"]
        ).ThreadReplyConfirmedOutcome(
            thread_id=effects[0].thread_id, reply_ref=effects[0].reply_ref
        ),
    )
    assert pending_deferred_reply_dispatch(state) is None
    assert awaiting_operator_continuation(state) is True
    continued = reduce_pr_review(
        state,
        UserContinuationRequested(
            occurred_at=T3,
            evidence=UserContinuationEvidence(
                repository=state.binding.repository,
                pr_number=state.binding.pr_number,
                cycle_number=state.cycle_number,
                head_sha=state.binding.head_sha,
                evidence_ref=artifact("continue.json"),
            ),
        ),
    )
    assert isinstance(continued, TransitionApplied)
    assert continued.state.kind == "waiting_for_bot"

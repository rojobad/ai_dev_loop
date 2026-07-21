"""Shared constants and path helpers for PR review v2 reducer tests."""

from __future__ import annotations

from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    ArtifactRef,
    CommitRecordedOutcome,
    EffectCompletionToken,
    EffectSucceeded,
    EligibleThreadsObservedOutcome,
    FrozenThreadSet,
    PrBoundOutcome,
    PrTextUpdatedOutcome,
    PublicationTextPreparedOutcome,
    PullRequestBinding,
    PushConfirmedOutcome,
    ReviewTriggerConfirmedOutcome,
    StartRequested,
    ThreadDecisionRecord,
    ThreadResolutionConfirmedOutcome,
    TransitionApplied,
    TriggerEvidence,
    reduce_pr_review,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64
HASH_3 = "3" * 64
T0 = "2026-07-20T12:00:00Z"
T1 = "2026-07-20T12:00:01Z"
T2 = "2026-07-20T12:00:02Z"
T3 = "2026-07-20T12:00:03Z"
T_LATER = "2026-07-20T12:10:00Z"
T_RETRY = "2026-07-20T12:00:30Z"
T_OFFSET_EQUIV = "2026-07-20T08:10:00-04:00"  # same instant as T_LATER


def artifact(path: str, digest: str = HASH_1) -> ArtifactRef:
    return ArtifactRef(relative_path=path, sha256=digest)


def token_for(effect, *, run_version: int = 1, lease_generation: int = 1) -> EffectCompletionToken:
    return EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=run_version,
        lease_generation=lease_generation,
        cycle_number=effect.cycle_number,
        bound_head_sha=effect.bound_head_sha,
    )


def apply(state, event):
    result = reduce_pr_review(state, event)
    assert isinstance(result, TransitionApplied), result
    return result.state, result.effects


def start(prepared):
    return apply(prepared, StartRequested(occurred_at=T1))


def succeed(state, effect, outcome, *, occurred_at: str = T2):
    return apply(
        state,
        EffectSucceeded(occurred_at=occurred_at, token=token_for(effect), outcome=outcome),
    )


def publication_text_outcome():
    return PublicationTextPreparedOutcome(
        publication_text_ref=artifact("artifacts/publication.md"),
        commit_message_ref=artifact("artifacts/commit-message.txt", HASH_2),
    )


def drive_initial_publication_to_waiting_for_bot(prepared):
    state, effects = start(prepared)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    state, effects = succeed(
        state,
        effects[0],
        CommitRecordedOutcome(commit_sha=SHA_B, new_head_sha=SHA_B),
    )
    state, effects = succeed(
        state,
        effects[0],
        PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature"),
    )
    binding = PullRequestBinding(
        repository=prepared.origin.repository,
        pr_number=7,
        head_branch=prepared.origin.head_branch,
        base_branch=prepared.origin.base_branch,
        head_sha=SHA_B,
    )
    state, effects = succeed(state, effects[0], PrBoundOutcome(binding=binding))
    return state, effects, binding


def confirm_trigger(state, effect, binding: PullRequestBinding):
    evidence = TriggerEvidence(
        marker=effect.marker,
        comment_ref=artifact("artifacts/trigger.json", HASH_3),
        head_sha=binding.head_sha,
    )
    return succeed(
        state,
        effect,
        ReviewTriggerConfirmedOutcome(evidence=evidence),
        occurred_at=T3,
    )


def freeze_threads(state, effect, binding: PullRequestBinding, thread_ids: tuple[str, ...]):
    assert state.trigger_evidence is not None
    frozen = FrozenThreadSet(
        thread_ids=thread_ids,
        snapshot_ref=artifact("artifacts/threads.json"),
        head_sha=binding.head_sha,
        cycle_number=state.cycle_number,
        trigger_marker=state.trigger_evidence.marker,
    )
    return succeed(
        state,
        effect,
        EligibleThreadsObservedOutcome(frozen=frozen),
        occurred_at=T_LATER,
    )


def actionable_adjudication(frozen: FrozenThreadSet) -> AdjudicationEvidence:
    decisions = tuple(
        ThreadDecisionRecord(
            thread_id=tid,
            decision=AdjudicationDecisionKind.ACTIONABLE,
            safe_summary=f"fix {tid}",
        )
        for tid in frozen.thread_ids
    )
    return AdjudicationEvidence(
        frozen=frozen,
        decisions=decisions,
        result_ref=artifact("artifacts/adjudication.json"),
        fix_prompt_ref=artifact("artifacts/fix-prompt.txt", HASH_2),
    )


def reply_adjudication(frozen: FrozenThreadSet) -> AdjudicationEvidence:
    decisions = tuple(
        ThreadDecisionRecord(
            thread_id=tid,
            decision=AdjudicationDecisionKind.NOT_APPLICABLE
            if i == 0
            else AdjudicationDecisionKind.UNCERTAIN,
            safe_summary=f"reply {tid}",
            reply_ref=artifact(f"artifacts/reply-{tid}.txt", HASH_3),
        )
        for i, tid in enumerate(frozen.thread_ids)
    )
    return AdjudicationEvidence(
        frozen=frozen,
        decisions=decisions,
        result_ref=artifact("artifacts/adjudication.json"),
        fix_prompt_ref=None,
    )


def drive_fix_publication(state, effects):
    """From publishing_fix generate_publication_text through resolve/cycle advance."""
    state, effects = succeed(state, effects[0], publication_text_outcome())
    new_sha = "e" * 40
    state, effects = succeed(
        state,
        effects[0],
        CommitRecordedOutcome(commit_sha=new_sha, new_head_sha=new_sha),
    )
    state, effects = succeed(
        state,
        effects[0],
        PushConfirmedOutcome(commit_sha=new_sha, remote_ref=state.binding.head_branch),
    )
    state, effects = succeed(
        state,
        effects[0],
        PrTextUpdatedOutcome(
            binding=state.binding,
            publication_text_ref=state.publication_text_ref or artifact("artifacts/publication.md"),
        ),
    )
    while state.kind == "publishing_fix" and effects:
        effect = effects[0]
        if effect.kind != "resolve_thread":
            break
        state, effects = succeed(
            state,
            effect,
            ThreadResolutionConfirmedOutcome(thread_id=effect.thread_id),
        )
    return state, effects

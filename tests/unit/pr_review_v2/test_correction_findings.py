"""Regression tests for Phase 16.3 correction findings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.helpers import (
    SHA_A,
    SHA_B,
    T1,
    T2,
    T_LATER,
    T_OFFSET_EQUIV,
    T_RETRY,
    artifact,
    confirm_trigger,
    publication_text_outcome,
    start,
    succeed,
    token_for,
)

from ai_dev_loop.pr_review_v2.domain import (
    PR_REVIEW_STATE_ADAPTER,
    BotStillWaitingOutcome,
    CommitRecordedOutcome,
    CompletedState,
    EffectRetryableFailure,
    EffectSucceeded,
    ErrorSummary,
    PauseReasonKind,
    PrBoundOutcome,
    PublicationStep,
    PullRequestBinding,
    PushConfirmedOutcome,
    ReconciliationResolutionKind,
    ReconciliationResolvedOutcome,
    ReconciliationStrategyKind,
    RejectionCode,
    RepositoryIdentity,
    ResumeRequested,
    RetryDue,
    TransientErrorKind,
    TransitionApplied,
    TransitionRejected,
    VerifiedNoFindingsEvidence,
    WriteOutcomeUncertain,
    coerce_utc_instant,
    parse_pr_review_effect,
    parse_pr_review_state,
    reduce_pr_review,
    with_attempt,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    CommitPatchEffect,
    ObserveBotReviewEffect,
    PostThreadReplyEffect,
    PushCommitEffect,
    ReconcileWriteEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    RunLocalFixEffect,
    UpdatePrTextEffect,
)
from ai_dev_loop.pr_review_v2.domain.state import ReconcilingWriteState


def test_mismatched_outcome_payload_with_valid_token_is_rejected(prepared_source) -> None:
    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    commit = effects[0]
    # Valid token and outcome kind, but SHA binding is wrong.
    result = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(commit),
            outcome=CommitRecordedOutcome(commit_sha=SHA_B, new_head_sha=SHA_A),
        ),
    )
    assert isinstance(result, TransitionRejected)
    assert result.code is RejectionCode.MISMATCHED_OUTCOME_BINDING
    assert not hasattr(result, "effects") or getattr(result, "effects", ()) == ()


def test_push_and_pr_binding_mismatches_rejected(prepared_source) -> None:
    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    state, effects = succeed(
        state, effects[0], CommitRecordedOutcome(commit_sha=SHA_B, new_head_sha=SHA_B)
    )
    push = effects[0]
    bad_push = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(push),
            outcome=PushConfirmedOutcome(commit_sha=SHA_A, remote_ref="feature"),
        ),
    )
    assert isinstance(bad_push, TransitionRejected)
    assert bad_push.code is RejectionCode.MISMATCHED_OUTCOME_BINDING

    state, effects = succeed(
        state, push, PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature")
    )
    create = effects[0]
    bad_pr = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(create),
            outcome=PrBoundOutcome(
                binding=PullRequestBinding(
                    repository=prepared_source.origin.repository,
                    pr_number=7,
                    head_branch="wrong-branch",
                    base_branch="main",
                    head_sha=SHA_B,
                )
            ),
        ),
    )
    assert isinstance(bad_pr, TransitionRejected)
    assert bad_pr.code is RejectionCode.MISMATCHED_OUTCOME_BINDING


def test_impossible_publishing_progress_rejected_at_model_boundary(prepared_source) -> None:
    state, effects = start(prepared_source)
    payload = PR_REVIEW_STATE_ADAPTER.dump_python(state)
    payload["step"] = PublicationStep.PUSH_COMMIT.value
    payload["active_effect"] = {
        "kind": "push_commit",
        "effect_id": "x",
        "idempotency_key": "x",
        "run_id": state.run_id,
        "cycle_number": 1,
        "attempt": 1,
        "max_attempts": 6,
        "repository": {"name_with_owner": "acme/demo"},
        "bound_head_sha": SHA_B,
        "commit_sha": SHA_B,
        "remote_ref": "feature",
        "force": False,
    }
    # Missing required publication refs / commit_sha for push step.
    with pytest.raises(ValidationError):
        parse_pr_review_state(payload)


def test_utc_instant_malformed_mixed_offset_equivalent_and_ordering() -> None:
    with pytest.raises(ValidationError):
        EffectRetryableFailure(
            occurred_at=T2,
            token=__import__(
                "ai_dev_loop.pr_review_v2.domain", fromlist=["EffectCompletionToken"]
            ).EffectCompletionToken(
                effect_id="e",
                expected_run_version=1,
                lease_generation=1,
                cycle_number=1,
                bound_head_sha=SHA_A,
            ),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="t"),
            failed_attempt=1,
            next_attempt_at="2026-07-20T12:00:02",  # naive / missing tz
        )
    later = coerce_utc_instant(T_LATER)
    equiv = coerce_utc_instant(T_OFFSET_EQUIV)
    assert later == equiv
    assert coerce_utc_instant(T1) < later


def test_retry_due_compares_instants_not_lexicographic_strings(prepared_source) -> None:
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
    # Equivalent instant via different offset is eligible.
    due = reduce_pr_review(
        waiting.state,
        RetryDue(
            occurred_at=T_OFFSET_EQUIV,
            pending_effect_id=waiting.state.retrying_effect_id,
            current_time=T_OFFSET_EQUIV,
        ),
    )
    assert isinstance(due, TransitionApplied)
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


def test_proven_not_applied_requires_future_retry_instant(prepared_source) -> None:
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
    with pytest.raises(ValidationError):
        ReconciliationResolvedOutcome(
            resolution=ReconciliationResolutionKind.PROVEN_NOT_APPLIED,
            original_effect_id=commit.effect_id,
        )
    not_future = reduce_pr_review(
        uncertain.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(uncertain.effects[0]),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.PROVEN_NOT_APPLIED,
                original_effect_id=commit.effect_id,
                next_attempt_at=T2,
            ),
        ),
    )
    assert isinstance(not_future, TransitionRejected)
    assert not_future.code is RejectionCode.RETRY_TIME_NOT_IN_FUTURE
    assert getattr(not_future, "effects", ()) == ()

    ok = reduce_pr_review(
        uncertain.state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(uncertain.effects[0]),
            outcome=ReconciliationResolvedOutcome(
                resolution=ReconciliationResolutionKind.PROVEN_NOT_APPLIED,
                original_effect_id=commit.effect_id,
                next_attempt_at=T_RETRY,
            ),
        ),
    )
    assert isinstance(ok, TransitionApplied)
    assert ok.state.kind == "waiting_retry"
    assert ok.effects == ()
    assert ok.state.retrying_effect_id == commit.effect_id


def test_bot_still_waiting_requires_exact_next_poll_sequence(prepared_existing) -> None:
    state, effects = start(prepared_existing)
    state, effects = confirm_trigger(state, effects[0], state.binding)
    observe = effects[0]
    current = state.poll_sequence
    stale = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(observe),
            outcome=BotStillWaitingOutcome(next_not_before=T_LATER, poll_sequence=current),
        ),
    )
    assert isinstance(stale, TransitionRejected)
    assert stale.code is RejectionCode.INVALID_POLL_SEQUENCE
    jump = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(observe),
            outcome=BotStillWaitingOutcome(next_not_before=T_LATER, poll_sequence=current + 2),
        ),
    )
    assert isinstance(jump, TransitionRejected)
    assert jump.code is RejectionCode.INVALID_POLL_SEQUENCE
    ok = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(observe),
            outcome=BotStillWaitingOutcome(next_not_before=T_LATER, poll_sequence=current + 1),
        ),
    )
    assert isinstance(ok, TransitionApplied)
    assert ok.state.poll_sequence == current + 1
    assert ok.effects[0].attempt == 1


def _commit_after_publication(prepared_source):
    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    return state, effects[0]


def test_reconcile_write_effect_requires_exact_original_identity(repo) -> None:
    commit = CommitPatchEffect(
        effect_id="e-commit",
        idempotency_key="e-commit",
        run_id="r1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=repo,
        bound_head_sha=SHA_A,
        patch_ref=artifact("p.patch"),
        expected_head_sha=SHA_A,
        commit_message_ref=artifact("m.txt"),
    )
    base = {
        "effect_id": "e-rec",
        "idempotency_key": "e-rec",
        "run_id": "r1",
        "cycle_number": 1,
        "attempt": 1,
        "max_attempts": 6,
        "repository": repo,
        "bound_head_sha": SHA_A,
        "original_write": commit,
        "strategy": ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD,
        "reconciliation_identity": "rec-1",
    }
    assert ReconcileWriteEffect(**base).original_write == commit

    with pytest.raises(ValidationError):
        ReconcileWriteEffect(
            **{
                **base,
                "strategy": ReconciliationStrategyKind.FIND_REMOTE_REF,
            }
        )
    with pytest.raises(ValidationError):
        ReconcileWriteEffect(**{**base, "run_id": "other-run"})
    with pytest.raises(ValidationError):
        ReconcileWriteEffect(**{**base, "cycle_number": 2})
    with pytest.raises(ValidationError):
        ReconcileWriteEffect(
            **{
                **base,
                "repository": RepositoryIdentity(name_with_owner="other/repo"),
            }
        )
    with pytest.raises(ValidationError):
        ReconcileWriteEffect(**{**base, "bound_head_sha": SHA_B})


def test_reconciling_write_state_requires_full_write_equality(prepared_source) -> None:
    state, commit = _commit_after_publication(prepared_source)
    divergent = commit.model_copy(update={"patch_ref": artifact("other.patch")})
    assert divergent.effect_id == commit.effect_id
    assert divergent != commit

    reconcile_ok = ReconcileWriteEffect(
        effect_id="e-rec",
        idempotency_key="e-rec",
        run_id=commit.run_id,
        cycle_number=commit.cycle_number,
        attempt=1,
        max_attempts=6,
        repository=commit.repository,
        bound_head_sha=commit.bound_head_sha,
        original_write=commit,
        strategy=ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD,
        reconciliation_identity="rec-1",
    )
    common = {
        "run_id": state.run_id,
        "origin": state.origin,
        "limits": state.limits,
        "cycle_number": state.cycle_number,
        "entered_at": T2,
        "suspended": state,
        "last_error": ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
    }
    assert (
        ReconcilingWriteState(
            **common,
            original_write=commit,
            active_effect=reconcile_ok,
        ).original_write
        == commit
    )

    with pytest.raises(ValidationError):
        ReconcilingWriteState(
            **common,
            original_write=divergent,
            active_effect=reconcile_ok.model_copy(update={"original_write": divergent}),
        )
    with pytest.raises(ValidationError):
        ReconcilingWriteState(
            **common,
            original_write=commit,
            active_effect=reconcile_ok.model_copy(update={"original_write": divergent}),
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda e: e.model_copy(update={"patch_ref": artifact("drift.patch")}),
        lambda e: e.model_copy(update={"expected_head_sha": SHA_B}),
        lambda e: e.model_copy(update={"attempt": 2}),
        lambda e: e.model_copy(update={"bound_head_sha": SHA_B}),
        lambda e: e.model_copy(
            update={"repository": RepositoryIdentity(name_with_owner="other/repo")}
        ),
        lambda e: e.model_copy(update={"commit_message_ref": artifact("other-msg.txt")}),
    ],
)
def test_uncertain_rejects_same_id_divergent_commit_writes(prepared_source, mutator) -> None:
    state, commit = _commit_after_publication(prepared_source)
    divergent = mutator(commit)
    assert divergent.effect_id == commit.effect_id
    assert divergent != commit
    result = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(commit),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-1",
            original_write=divergent,
        ),
    )
    assert isinstance(result, TransitionRejected)
    assert result.code is RejectionCode.MISMATCHED_ORIGINAL_WRITE
    assert getattr(result, "effects", ()) == ()


def test_uncertain_rejects_same_id_divergent_push_and_marker_writes(
    prepared_source, binding
) -> None:
    state, effects = start(prepared_source)
    state, effects = succeed(state, effects[0], publication_text_outcome())
    state, effects = succeed(
        state, effects[0], CommitRecordedOutcome(commit_sha=SHA_B, new_head_sha=SHA_B)
    )
    push = effects[0]
    assert isinstance(push, PushCommitEffect)
    bad_push = push.model_copy(update={"remote_ref": "other-branch"})
    assert bad_push.effect_id == push.effect_id
    rejected_push = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(push),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-push",
            original_write=bad_push,
        ),
    )
    assert isinstance(rejected_push, TransitionRejected)
    assert rejected_push.code is RejectionCode.MISMATCHED_ORIGINAL_WRITE
    assert getattr(rejected_push, "effects", ()) == ()

    state, effects = succeed(
        state, push, PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature")
    )
    state, effects = succeed(
        state,
        effects[0],
        PrBoundOutcome(binding=binding.model_copy(update={"head_sha": SHA_B})),
    )
    request = effects[0]
    assert isinstance(request, RequestBotReviewEffect)
    bad_marker = request.model_copy(update={"marker": "other-marker"})
    bad_binding = request.model_copy(
        update={"binding": binding.model_copy(update={"head_sha": SHA_B, "pr_number": 99})}
    )
    for divergent in (bad_marker, bad_binding):
        assert divergent.effect_id == request.effect_id
        rejected = reduce_pr_review(
            state,
            WriteOutcomeUncertain(
                occurred_at=T2,
                token=token_for(request),
                error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
                reconciliation_identity="rec-request",
                original_write=divergent,
            ),
        )
        assert isinstance(rejected, TransitionRejected)
        assert rejected.code is RejectionCode.MISMATCHED_ORIGINAL_WRITE
        assert getattr(rejected, "effects", ()) == ()


def test_uncertain_rejects_same_id_divergent_thread_writes(prepared_source) -> None:
    from tests.unit.pr_review_v2.helpers import (
        drive_initial_publication_to_waiting_for_bot,
        freeze_threads,
        reply_adjudication,
    )

    from ai_dev_loop.pr_review_v2.domain import AdjudicationRecordedOutcome
    from ai_dev_loop.pr_review_v2.domain.effects import PostThreadReplyEffect

    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("t1", "t2"))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(state.frozen)),
    )
    reply = effects[0]
    assert isinstance(reply, PostThreadReplyEffect)
    divergent = reply.model_copy(update={"thread_id": "t-other"})
    assert divergent.effect_id == reply.effect_id
    assert divergent != reply
    rejected = reduce_pr_review(
        state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(reply),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-reply",
            original_write=divergent,
        ),
    )
    assert isinstance(rejected, TransitionRejected)
    assert rejected.code is RejectionCode.MISMATCHED_ORIGINAL_WRITE
    assert getattr(rejected, "effects", ()) == ()


def test_with_attempt_preserves_validation_and_identity(prepared_source) -> None:
    _, commit = _commit_after_publication(prepared_source)
    for bad in (0, -1, commit.max_attempts + 1):
        with pytest.raises(ValueError):
            with_attempt(commit, bad)

    retried = with_attempt(commit, 2)
    resumed = with_attempt(commit, 1)
    assert isinstance(retried, CommitPatchEffect)
    assert isinstance(resumed, CommitPatchEffect)
    assert retried.attempt == 2
    assert resumed.attempt == 1
    assert retried.effect_id == commit.effect_id == resumed.effect_id
    assert retried.idempotency_key == commit.idempotency_key == resumed.idempotency_key
    assert retried.model_dump(exclude={"attempt"}) == commit.model_dump(exclude={"attempt"})
    assert resumed.model_dump(exclude={"attempt"}) == commit.model_dump(exclude={"attempt"})

    at_max = with_attempt(commit, commit.max_attempts)
    assert at_max.attempt == commit.max_attempts
    assert at_max.effect_id == commit.effect_id
    assert at_max.idempotency_key == commit.idempotency_key


def _binding_effect_bases(binding: PullRequestBinding) -> dict:
    return {
        "effect_id": "e-bind",
        "idempotency_key": "e-bind",
        "run_id": "r1",
        "cycle_number": 1,
        "attempt": 1,
        "max_attempts": 6,
        "repository": binding.repository,
        "bound_head_sha": binding.head_sha,
        "binding": binding,
    }


@pytest.mark.parametrize(
    ("effect_cls", "extra"),
    [
        (RequestBotReviewEffect, {"marker": "marker"}),
        (ObserveBotReviewEffect, {"poll_sequence": 1, "trigger_marker": "marker"}),
        (
            AdjudicateThreadsEffect,
            {
                "frozen_thread_ids": ("t1",),
                "snapshot_ref": artifact("snap.json"),
                "execution_context_ref": artifact("ctx.json"),
            },
        ),
        (PostThreadReplyEffect, {"thread_id": "t1", "reply_ref": artifact("reply.json")}),
        (
            RunLocalFixEffect,
            {
                "actionable_thread_ids": ("t1",),
                "fix_prompt_ref": artifact("fix.txt"),
                "execution_context_ref": artifact("ctx.json"),
            },
        ),
        (UpdatePrTextEffect, {"publication_text_ref": artifact("pub.md")}),
        (ResolveThreadEffect, {"thread_id": "t1"}),
    ],
)
def test_binding_bearing_effects_reject_repo_and_sha_drift(binding, effect_cls, extra) -> None:
    base = {**_binding_effect_bases(binding), **extra}
    assert effect_cls(**base).binding == binding

    other_repo = RepositoryIdentity(name_with_owner="other/repo")
    with pytest.raises(ValidationError):
        effect_cls(**{**base, "repository": other_repo})
    with pytest.raises(ValidationError):
        effect_cls(**{**base, "bound_head_sha": SHA_B})

    payload = effect_cls(**base).model_dump(mode="python")
    payload["repository"] = {"name_with_owner": "other/repo"}
    with pytest.raises(ValidationError):
        parse_pr_review_effect(payload)
    payload = effect_cls(**base).model_dump(mode="python")
    payload["bound_head_sha"] = SHA_B
    with pytest.raises(ValidationError):
        parse_pr_review_effect(payload)


def test_completed_state_requires_evidence_head_sha(binding, prepared_source) -> None:
    evidence = VerifiedNoFindingsEvidence(
        head_sha=binding.head_sha,
        observation_ref=artifact("obs.json"),
        verified_at=T2,
    )
    completed = CompletedState(
        run_id=prepared_source.run_id,
        origin=prepared_source.origin,
        limits=prepared_source.limits,
        binding=binding,
        cycle_number=1,
        evidence=evidence,
        completed_at=T2,
    )
    assert completed.evidence.head_sha == binding.head_sha
    restored = parse_pr_review_state(completed.model_dump(mode="python"))
    assert restored == completed

    with pytest.raises(ValidationError):
        CompletedState(
            run_id=prepared_source.run_id,
            origin=prepared_source.origin,
            limits=prepared_source.limits,
            binding=binding,
            cycle_number=1,
            evidence=evidence.model_copy(update={"head_sha": SHA_B}),
            completed_at=T2,
        )
    payload = completed.model_dump(mode="python")
    payload["evidence"]["head_sha"] = SHA_B
    with pytest.raises(ValidationError):
        parse_pr_review_state(payload)
    with pytest.raises(ValidationError):
        PR_REVIEW_STATE_ADAPTER.validate_python(payload)


def test_nested_envelope_mismatches_rejected_via_serialization(prepared_source) -> None:
    state, effects = start(prepared_source)
    effect = effects[0]
    waiting = reduce_pr_review(
        state,
        EffectRetryableFailure(
            occurred_at=T2,
            token=token_for(effect),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
            failed_attempt=1,
            next_attempt_at=T_LATER,
        ),
    )
    assert isinstance(waiting, TransitionApplied)
    assert waiting.state.kind == "waiting_retry"
    payload = waiting.state.model_dump(mode="python")

    for field, value in (
        ("run_id", "other-run"),
        ("cycle_number", 9),
        ("limits", {"max_external_cycles": 99, "max_local_iterations": 3}),
    ):
        bad = dict(payload)
        bad[field] = value
        with pytest.raises(ValidationError):
            parse_pr_review_state(bad)

    bad_origin = dict(payload)
    bad_origin["origin"] = {
        **payload["origin"],
        "repository": {"name_with_owner": "other/repo"},
    }
    with pytest.raises(ValidationError):
        parse_pr_review_state(bad_origin)

    # Exhaust retries to obtain a paused envelope with resumable nested state.
    exhausted = state
    active = effect
    for attempt in range(1, effect.max_attempts + 1):
        result = reduce_pr_review(
            exhausted,
            EffectRetryableFailure(
                occurred_at=T2,
                token=token_for(active),
                error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
                failed_attempt=attempt,
                next_attempt_at=T_LATER,
            ),
        )
        assert isinstance(result, TransitionApplied)
        if result.state.kind == "paused":
            paused = result.state
            break
        assert result.state.kind == "waiting_retry"
        due = reduce_pr_review(
            result.state,
            RetryDue(
                occurred_at=T_LATER,
                pending_effect_id=result.state.retrying_effect_id,
                current_time=T_LATER,
            ),
        )
        assert isinstance(due, TransitionApplied)
        exhausted = due.state
        active = due.effects[0]
    else:
        raise AssertionError("expected retry exhaustion pause")

    paused_payload = paused.model_dump(mode="python")
    bad_paused = dict(paused_payload)
    bad_paused["run_id"] = "foreign-run"
    with pytest.raises(ValidationError):
        parse_pr_review_state(bad_paused)
    bad_paused = dict(paused_payload)
    bad_paused["cycle_number"] = 7
    with pytest.raises(ValidationError):
        parse_pr_review_state(bad_paused)
    bad_paused = dict(paused_payload)
    bad_paused["origin"] = {
        **paused_payload["origin"],
        "repository": {"name_with_owner": "other/repo"},
    }
    with pytest.raises(ValidationError):
        parse_pr_review_state(bad_paused)

    # Binding mismatch on a reconciling envelope (binding present after push/PR).
    pub_state, commit = _commit_after_publication(prepared_source)
    uncertain = reduce_pr_review(
        pub_state,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(commit),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-env",
            original_write=commit,
        ),
    )
    assert isinstance(uncertain, TransitionApplied)
    reconciling = uncertain.state
    assert reconciling.kind == "reconciling_write"
    recon_payload = reconciling.model_dump(mode="python")
    bad_recon = dict(recon_payload)
    bad_recon["run_id"] = "cross-run"
    with pytest.raises(ValidationError):
        parse_pr_review_state(bad_recon)
    # Inject a binding that does not match suspended (publishing_initial has none).
    bad_recon = dict(recon_payload)
    bad_recon["binding"] = {
        "repository": {"name_with_owner": "acme/demo"},
        "pr_number": 1,
        "head_branch": "feature",
        "base_branch": "main",
        "head_sha": SHA_A,
    }
    with pytest.raises(ValidationError):
        parse_pr_review_state(bad_recon)


def test_retry_and_resume_emit_only_same_run_effects(prepared_source) -> None:
    state, effects = start(prepared_source)
    effect = effects[0]
    waiting = reduce_pr_review(
        state,
        EffectRetryableFailure(
            occurred_at=T2,
            token=token_for(effect),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
            failed_attempt=1,
            next_attempt_at=T_LATER,
        ),
    )
    assert isinstance(waiting, TransitionApplied)
    due = reduce_pr_review(
        waiting.state,
        RetryDue(
            occurred_at=T_LATER,
            pending_effect_id=waiting.state.retrying_effect_id,
            current_time=T_LATER,
        ),
    )
    assert isinstance(due, TransitionApplied)
    assert due.effects[0].run_id == prepared_source.run_id == due.state.run_id
    assert due.effects[0].run_id == due.state.active_effect.run_id
    assert due.effects[0].effect_id == effect.effect_id

    exhausted = due.state
    active = due.effects[0]
    for attempt in range(active.attempt, active.max_attempts + 1):
        result = reduce_pr_review(
            exhausted,
            EffectRetryableFailure(
                occurred_at=T2,
                token=token_for(active),
                error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
                failed_attempt=attempt,
                next_attempt_at=T_LATER,
            ),
        )
        assert isinstance(result, TransitionApplied)
        if result.state.kind == "paused":
            assert result.state.reason is PauseReasonKind.RETRY_EXHAUSTED
            resumed = reduce_pr_review(result.state, ResumeRequested(occurred_at=T2))
            assert isinstance(resumed, TransitionApplied)
            assert resumed.effects[0].run_id == prepared_source.run_id
            assert resumed.state.run_id == prepared_source.run_id
            assert resumed.effects[0].effect_id == effect.effect_id
            assert resumed.effects[0].attempt == 1
            return
        due = reduce_pr_review(
            result.state,
            RetryDue(
                occurred_at=T_LATER,
                pending_effect_id=result.state.retrying_effect_id,
                current_time=T_LATER,
            ),
        )
        assert isinstance(due, TransitionApplied)
        exhausted = due.state
        active = due.effects[0]
    raise AssertionError("expected resume after exhaustion")

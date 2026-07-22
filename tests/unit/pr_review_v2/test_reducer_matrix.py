"""Exhaustive state/event-kind classification and fencing rejection tests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.helpers import (
    SHA_A,
    SHA_B,
    T1,
    T2,
    T_LATER,
    actionable_adjudication,
    artifact,
    confirm_trigger,
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
    TRANSITION_REGISTRY,
    AbortRequested,
    AdjudicationRecordedOutcome,
    EffectBlocked,
    EffectCompletionToken,
    EffectRetryableFailure,
    EffectSucceeded,
    ErrorSummary,
    FailureReasonKind,
    FatalFailureDetected,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PauseReasonKind,
    RejectionCode,
    ResumeRequested,
    RetryDue,
    SafeAction,
    SafeActionKind,
    StartRequested,
    TransientErrorKind,
    TransitionApplied,
    TransitionRejected,
    UserContinuationEvidence,
    UserContinuationRequested,
    VerifiedNoFindingsEvidence,
    VerifiedNoFindingsOutcome,
    WriteOutcomeUncertain,
    accepted_transition_pairs,
    active_effect,
    reduce_pr_review,
)
from ai_dev_loop.pr_review_v2.domain.effects import CommitPatchEffect
from ai_dev_loop.pr_review_v2.domain.reducer import EVENT_KINDS, STATE_KINDS

_STATE_CACHE: dict[int, dict[str, object]] = {}


def _states(prepared_source, prepared_existing):
    key = id(prepared_source) ^ id(prepared_existing)
    cached = _STATE_CACHE.get(key)
    if cached is not None:
        return cached
    built = _build_states(prepared_source, prepared_existing)
    _STATE_CACHE[key] = built
    return built


def _build_states(prepared_source, prepared_existing):
    prepared = prepared_source
    publishing, pub_effects = start(prepared_source)
    waiting, wait_effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    waiting_obs, obs_effects = confirm_trigger(waiting, wait_effects[0], binding)
    adjudicating, adj_effects = freeze_threads(
        waiting_obs, obs_effects[0], binding, ("thread-a", "thread-b")
    )
    running, run_effects = succeed(
        adjudicating,
        adj_effects[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(adjudicating.frozen)),
    )
    waiting_u, obs_u = confirm_trigger(waiting, wait_effects[0], binding)
    adjudicating_u, adj_u = freeze_threads(waiting_u, obs_u[0], binding, ("t1", "t2"))
    waiting_user, _ = succeed(
        adjudicating_u,
        adj_u[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(adjudicating_u.frozen)),
    )
    publishing_fix, _ = succeed(
        running,
        run_effects[0],
        LocalFixFinishedOutcome(
            outcome=LocalFixOutcomeKind.ACCEPTED,
            accepted_patch_ref=artifact("artifacts/fix.patch"),
            new_head_sha="e" * 40,
            result_ref=artifact("artifacts/local-result.json"),
        ),
    )
    retry = reduce_pr_review(
        publishing,
        EffectRetryableFailure(
            occurred_at=T2,
            token=token_for(pub_effects[0]),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
            failed_attempt=1,
            next_attempt_at=T_LATER,
        ),
    )
    assert isinstance(retry, TransitionApplied)
    state_c, effects_c = succeed(publishing, pub_effects[0], publication_text_outcome())
    uncertain = reduce_pr_review(
        state_c,
        WriteOutcomeUncertain(
            occurred_at=T2,
            token=token_for(effects_c[0]),
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-commit-1",
            original_write=effects_c[0],
        ),
    )
    assert isinstance(uncertain, TransitionApplied)
    blocked = reduce_pr_review(
        publishing,
        EffectBlocked(
            occurred_at=T2,
            token=token_for(pub_effects[0]),
            reason=PauseReasonKind.PERMISSIONS,
            safe_action=SafeAction(
                kind=SafeActionKind.FIX_PERMISSIONS_THEN_RESUME, condition="fix perms"
            ),
            safe_summary="permissions",
        ),
    )
    assert isinstance(blocked, TransitionApplied)
    completed = reduce_pr_review(
        waiting_obs,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(obs_effects[0]),
            outcome=VerifiedNoFindingsOutcome(
                evidence=VerifiedNoFindingsEvidence(
                    head_sha=binding.head_sha,
                    observation_ref=artifact("obs.json"),
                    verified_at=T2,
                )
            ),
        ),
    )
    assert isinstance(completed, TransitionApplied)
    failed = reduce_pr_review(
        prepared,
        FatalFailureDetected(
            occurred_at=T2,
            reason=FailureReasonKind.INTERNAL_CORRUPTION,
            safe_summary="corrupt",
        ),
    )
    assert isinstance(failed, TransitionApplied)
    aborted = reduce_pr_review(prepared_existing, AbortRequested(occurred_at=T2))
    assert isinstance(aborted, TransitionApplied)
    return {
        "prepared": prepared,
        "publishing_initial": publishing,
        "waiting_for_bot": waiting_obs,
        "adjudicating": adjudicating,
        "waiting_for_user": waiting_user,
        "running_local_fix": running,
        "publishing_fix": publishing_fix,
        "reconciling_write": uncertain.state,
        "waiting_retry": retry.state,
        "completed": completed.state,
        "paused": blocked.state,
        "failed": failed.state,
        "aborted": aborted.state,
    }


def _event_for(state_kind: str, event_kind: str, state, prepared_source):
    if event_kind == "start_requested":
        return StartRequested(occurred_at=T1)
    if event_kind == "resume_requested":
        return ResumeRequested(occurred_at=T1)
    if event_kind == "retry_due":
        pending = getattr(state, "retrying_effect_id", "missing")
        return RetryDue(occurred_at=T_LATER, pending_effect_id=pending, current_time=T_LATER)
    if event_kind == "user_continuation_requested":
        binding = getattr(state, "binding", None)
        return UserContinuationRequested(
            occurred_at=T1,
            evidence=UserContinuationEvidence(
                repository=prepared_source.origin.repository,
                pr_number=binding.pr_number if binding else 1,
                cycle_number=getattr(state, "cycle_number", 1),
                head_sha=binding.head_sha if binding else SHA_A,
                evidence_ref=artifact("u.json"),
            ),
        )
    if event_kind == "abort_requested":
        return AbortRequested(occurred_at=T1)
    if event_kind == "fatal_failure_detected":
        return FatalFailureDetected(
            occurred_at=T1,
            reason=FailureReasonKind.INVARIANT_VIOLATION,
            safe_summary="fatal",
        )
    effect = active_effect(state)
    tok = (
        token_for(effect)
        if effect is not None
        else EffectCompletionToken(
            effect_id="missing",
            expected_run_version=1,
            lease_generation=1,
            cycle_number=1,
            bound_head_sha=SHA_A,
        )
    )
    if event_kind == "effect_succeeded":
        return EffectSucceeded(occurred_at=T1, token=tok, outcome=publication_text_outcome())
    if event_kind == "effect_retryable_failure":
        return EffectRetryableFailure(
            occurred_at=T1,
            token=tok,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="t"),
            failed_attempt=effect.attempt if effect is not None else 1,
            next_attempt_at=T_LATER,
        )
    if event_kind == "effect_blocked":
        return EffectBlocked(
            occurred_at=T1,
            token=tok,
            reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            safe_action=SafeAction(kind=SafeActionKind.INSPECT_ARTIFACTS, condition="inspect"),
            safe_summary="blocked",
        )
    if event_kind == "write_outcome_uncertain":
        original = (
            effect
            if effect is not None and effect.kind == "commit_patch"
            else CommitPatchEffect(
                effect_id=tok.effect_id,
                idempotency_key=tok.effect_id,
                run_id=state.run_id,
                cycle_number=1,
                attempt=1,
                max_attempts=6,
                repository=prepared_source.origin.repository,
                bound_head_sha=SHA_A,
                patch_ref=artifact("p.patch"),
                expected_head_sha=SHA_A,
                expected_branch="feature",
                commit_message_ref=artifact("m.txt"),
            )
        )
        return WriteOutcomeUncertain(
            occurred_at=T1,
            token=tok,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="a"),
            reconciliation_identity="rec",
            original_write=original,
        )
    raise AssertionError(event_kind)


def test_registry_covers_every_state_event_pair() -> None:
    assert set(TRANSITION_REGISTRY) == {(sk, ek) for sk in STATE_KINDS for ek in EVENT_KINDS}


@pytest.mark.parametrize("state_kind", STATE_KINDS)
@pytest.mark.parametrize("event_kind", EVENT_KINDS)
def test_every_pair_classified_without_mutating_input(
    state_kind: str,
    event_kind: str,
    prepared_source,
    prepared_existing,
) -> None:
    states = _states(prepared_source, prepared_existing)
    state = states[state_kind]
    event = _event_for(state_kind, event_kind, state, prepared_source)
    before = PR_REVIEW_STATE_ADAPTER.dump_json(state)
    result = reduce_pr_review(state, event)
    assert before == PR_REVIEW_STATE_ADAPTER.dump_json(state)
    assert isinstance(result, (TransitionApplied, TransitionRejected))
    if (state_kind, event_kind) not in accepted_transition_pairs():
        assert isinstance(result, TransitionRejected)
    if isinstance(result, TransitionRejected):
        assert "state" not in json.loads(result.model_dump_json())


def test_stale_effect_cycle_sha_rejected(prepared_source) -> None:
    state, effects = start(prepared_source)
    effect = effects[0]
    cases = [
        (
            token_for(effect).model_copy(update={"effect_id": "other"}),
            RejectionCode.STALE_EFFECT_ID,
        ),
        (token_for(effect).model_copy(update={"cycle_number": 9}), RejectionCode.STALE_CYCLE),
        (
            token_for(effect).model_copy(update={"bound_head_sha": SHA_B}),
            RejectionCode.STALE_HEAD_SHA,
        ),
    ]
    for tok, code in cases:
        result = reduce_pr_review(
            state,
            EffectSucceeded(occurred_at=T2, token=tok, outcome=publication_text_outcome()),
        )
        assert isinstance(result, TransitionRejected)
        assert result.code is code


def test_token_shape_requires_positive_run_version_and_lease(prepared_source) -> None:
    """Phase 16.3 validates token field shape; transactional fencing is Phase 16.4."""
    _, effects = start(prepared_source)
    with pytest.raises(ValidationError):
        token_for(effects[0], run_version=0)
    with pytest.raises(ValidationError):
        token_for(effects[0], lease_generation=0)


def test_terminal_states_reject_all_events(prepared_source, prepared_existing) -> None:
    states = _states(prepared_source, prepared_existing)
    for kind in ("completed", "failed", "aborted"):
        state = states[kind]
        for event_kind in EVENT_KINDS:
            event = _event_for(kind, event_kind, state, prepared_source)
            result = reduce_pr_review(state, event)
            assert isinstance(result, TransitionRejected)
            assert result.code is RejectionCode.TERMINAL_STATE


def test_mismatched_outcome_kind_rejected(prepared_source) -> None:
    state, effects = start(prepared_source)
    result = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(effects[0]),
            outcome=VerifiedNoFindingsOutcome(
                evidence=VerifiedNoFindingsEvidence(
                    head_sha=SHA_A,
                    observation_ref=artifact("obs.json"),
                    verified_at=T2,
                )
            ),
        ),
    )
    assert isinstance(result, TransitionRejected)
    assert result.code is RejectionCode.MISMATCHED_OUTCOME_KIND


def test_abort_from_every_non_terminal(prepared_source, prepared_existing) -> None:
    states = _states(prepared_source, prepared_existing)
    for kind, state in states.items():
        if kind in {"completed", "failed", "aborted"}:
            continue
        result = reduce_pr_review(state, AbortRequested(occurred_at=T2))
        assert isinstance(result, TransitionApplied)
        assert result.state.kind == "aborted"
        assert result.effects == ()

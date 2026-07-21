"""JSON round-trip and discriminator schema tests for PR review v2 unions."""

from __future__ import annotations

import json

from tests.unit.pr_review_v2.helpers import (
    HASH_1,
    T1,
    T2,
    T_LATER,
    artifact,
    drive_initial_publication_to_waiting_for_bot,
    start,
    token_for,
)

from ai_dev_loop.pr_review_v2.domain import (
    PR_REVIEW_EFFECT_ADAPTER,
    PR_REVIEW_EVENT_ADAPTER,
    PR_REVIEW_STATE_ADAPTER,
    AbortedState,
    AbortRequested,
    CompletedState,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    ErrorSummary,
    ExistingPrOrigin,
    FailedState,
    FailureReasonKind,
    FatalFailureDetected,
    PausedState,
    PauseReasonKind,
    PublicationTextPreparedOutcome,
    SafeAction,
    SafeActionKind,
    StartRequested,
    TransientErrorKind,
    VerifiedNoFindingsEvidence,
    WaitingRetryState,
    parse_pr_review_effect,
    parse_pr_review_event,
    parse_pr_review_state,
    reduce_pr_review,
)


def _round_trip_state(state):
    payload = json.loads(PR_REVIEW_STATE_ADAPTER.dump_json(state))
    assert "kind" in payload
    restored = parse_pr_review_state(payload)
    assert restored == state
    assert PR_REVIEW_STATE_ADAPTER.dump_json(restored) == PR_REVIEW_STATE_ADAPTER.dump_json(state)


def _round_trip_event(event):
    payload = json.loads(PR_REVIEW_EVENT_ADAPTER.dump_json(event))
    assert "kind" in payload
    restored = parse_pr_review_event(payload)
    assert restored == event


def _round_trip_effect(effect):
    payload = json.loads(PR_REVIEW_EFFECT_ADAPTER.dump_json(effect))
    assert "kind" in payload
    restored = parse_pr_review_effect(payload)
    assert restored == effect


def test_prepared_and_start_round_trip(prepared_source) -> None:
    _round_trip_state(prepared_source)
    _round_trip_event(StartRequested(occurred_at=T1))
    state, effects = start(prepared_source)
    _round_trip_state(state)
    _round_trip_effect(effects[0])


def test_operational_and_terminal_round_trips(prepared_source, prepared_existing) -> None:
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    _round_trip_state(state)
    _round_trip_effect(effects[0])
    completed = CompletedState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=binding,
        cycle_number=1,
        evidence=VerifiedNoFindingsEvidence(
            head_sha=binding.head_sha,
            observation_ref=artifact("obs.json"),
            verified_at=T2,
        ),
        completed_at=T2,
    )
    _round_trip_state(completed)
    _round_trip_state(
        FailedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=1,
            reason=FailureReasonKind.INVARIANT_VIOLATION,
            safe_summary="broken",
            failed_at=T2,
            binding=binding,
        )
    )
    _round_trip_state(
        AbortedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            cycle_number=1,
            reason="user_requested_abort",
            aborted_at=T2,
            binding=binding,
        )
    )
    existing_state, existing_effects = start(prepared_existing)
    _round_trip_state(existing_state)
    assert isinstance(existing_state.origin, ExistingPrOrigin)


def test_event_variant_round_trips(prepared_source) -> None:
    state, effects = start(prepared_source)
    effect = effects[0]
    tok = token_for(effect)
    samples = [
        EffectSucceeded(
            occurred_at=T2,
            token=tok,
            outcome=PublicationTextPreparedOutcome(
                publication_text_ref=artifact("p.md"),
                commit_message_ref=artifact("m.txt", HASH_1),
            ),
        ),
        EffectRetryableFailure(
            occurred_at=T2,
            token=tok,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
            failed_attempt=1,
            next_attempt_at=T_LATER,
        ),
        EffectBlocked(
            occurred_at=T2,
            token=tok,
            reason=PauseReasonKind.AUTHENTICATION,
            safe_action=SafeAction(kind=SafeActionKind.FIX_AUTH_THEN_RESUME, condition="fix auth"),
            safe_summary="auth",
        ),
        AbortRequested(occurred_at=T2),
        FatalFailureDetected(
            occurred_at=T2,
            reason=FailureReasonKind.CORRUPT_ARTIFACT,
            safe_summary="corrupt",
        ),
    ]
    for sample in samples:
        _round_trip_event(sample)


def test_generated_schemas_include_discriminators() -> None:
    for adapter in (
        PR_REVIEW_STATE_ADAPTER,
        PR_REVIEW_EVENT_ADAPTER,
        PR_REVIEW_EFFECT_ADAPTER,
    ):
        schema = adapter.json_schema()
        text = json.dumps(schema)
        assert "kind" in text
        assert "oneOf" in text or "anyOf" in text or "discriminator" in text


def test_paused_and_retry_round_trip(prepared_source) -> None:
    state, effects = start(prepared_source)
    effect = effects[0]
    result = reduce_pr_review(
        state,
        EffectRetryableFailure(
            occurred_at=T2,
            token=token_for(effect),
            error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="503"),
            failed_attempt=1,
            next_attempt_at=T_LATER,
        ),
    )
    assert isinstance(result.state, type(result.state))
    from ai_dev_loop.pr_review_v2.domain import TransitionApplied

    assert isinstance(result, TransitionApplied)
    assert isinstance(result.state, WaitingRetryState)
    _round_trip_state(result.state)
    paused = PausedState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        cycle_number=1,
        reason=PauseReasonKind.RETRY_EXHAUSTED,
        safe_action=SafeAction(kind=SafeActionKind.RESUME_SAME_EFFECT, condition="resume"),
        safe_summary="exhausted",
        paused_at=T2,
        resumable=state,
    )
    _round_trip_state(paused)

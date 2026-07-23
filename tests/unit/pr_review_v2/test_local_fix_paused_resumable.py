"""Unit tests for PAUSED local-fix resumable envelope behavior."""

from __future__ import annotations

from tests.unit.pr_review_v2.helpers import (
    T2,
    actionable_adjudication,
    artifact,
    confirm_trigger,
    drive_initial_publication_to_waiting_for_bot,
    freeze_threads,
    succeed,
    token_for,
)

from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationRecordedOutcome,
    EffectSucceeded,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PauseReasonKind,
    SafeAction,
    SafeActionKind,
    TransitionApplied,
    reduce_pr_review,
)


def _running_local_fix(prepared_source):
    state, effects, binding = drive_initial_publication_to_waiting_for_bot(prepared_source)
    state, effects = confirm_trigger(state, effects[0], binding)
    state, effects = freeze_threads(state, effects[0], binding, ("fix-1",))
    state, effects = succeed(
        state,
        effects[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(state.frozen)),
    )
    assert state.kind == "running_local_fix"
    return state, effects[0]


def test_paused_local_fix_sets_resumable_running_state(prepared_source) -> None:
    state, effect = _running_local_fix(prepared_source)
    result = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(effect),
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
    assert isinstance(result, TransitionApplied)
    assert result.state.kind == "paused"
    assert result.state.resumable is not None
    assert result.state.resumable.kind == "running_local_fix"
    assert result.state.resumable.run_id == state.run_id


def test_failed_local_fix_is_not_resumable(prepared_source) -> None:
    state, effect = _running_local_fix(prepared_source)
    result = reduce_pr_review(
        state,
        EffectSucceeded(
            occurred_at=T2,
            token=token_for(effect),
            outcome=LocalFixFinishedOutcome(
                outcome=LocalFixOutcomeKind.FAILED,
                result_ref=artifact("artifacts/local-result.json"),
                pause_reason=PauseReasonKind.LOCAL_FIX_FAILED,
                safe_action=SafeAction(
                    kind=SafeActionKind.INSPECT_ARTIFACTS,
                    condition="inspect failure",
                ),
            ),
        ),
    )
    assert isinstance(result, TransitionApplied)
    assert result.state.kind == "paused"
    assert result.state.resumable is None

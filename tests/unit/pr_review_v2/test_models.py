"""Model construction, immutability, and invariant tests for PR review v2."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.helpers import HASH_1, SHA_A, artifact

from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationDecisionKind,
    AdjudicationEvidence,
    ArtifactRef,
    EffectCompletionToken,
    ErrorSummary,
    FailureReasonKind,
    FrozenThreadSet,
    PauseReasonKind,
    PullRequestBinding,
    RepositoryIdentity,
    SafeAction,
    SafeActionKind,
    ThreadDecisionRecord,
    TransientErrorKind,
    WorkflowLimits,
    build_effect_identity,
    classify_effect,
    is_mutating_effect,
    is_read_only_effect,
    is_reconciling_effect,
    stable_effect_ids,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    CommitPatchEffect,
    ObserveBotReviewEffect,
    ReconcileWriteEffect,
    RequestBotReviewEffect,
)


def test_artifact_ref_rejects_unsafe_paths() -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(relative_path="../escape.txt", sha256=HASH_1)
    with pytest.raises(ValidationError):
        ArtifactRef(relative_path="/abs.txt", sha256=HASH_1)
    with pytest.raises(ValidationError):
        ArtifactRef(relative_path="a\\b.txt", sha256=HASH_1)


def test_sha_constraints() -> None:
    with pytest.raises(ValidationError):
        EffectCompletionToken(
            effect_id="e1",
            expected_run_version=1,
            lease_generation=1,
            cycle_number=1,
            bound_head_sha="ABC",
        )
    with pytest.raises(ValidationError):
        ArtifactRef(relative_path="ok.txt", sha256="Z" * 64)


def test_workflow_limits_reject_zero_and_non_six_attempts() -> None:
    with pytest.raises(ValidationError):
        WorkflowLimits(max_external_cycles=0, max_local_iterations=1)
    with pytest.raises(ValidationError):
        WorkflowLimits(
            max_external_cycles=1, max_local_iterations=1, github_max_attempts_per_batch=5
        )


def test_frozen_thread_set_unique_nonempty() -> None:
    with pytest.raises(ValidationError):
        FrozenThreadSet(
            thread_ids=(),
            snapshot_ref=artifact("t.json"),
            head_sha=SHA_A,
            cycle_number=1,
            trigger_marker="m",
        )
    with pytest.raises(ValidationError):
        FrozenThreadSet(
            thread_ids=("t1", "t1"),
            snapshot_ref=artifact("t.json"),
            head_sha=SHA_A,
            cycle_number=1,
            trigger_marker="m",
        )


def test_adjudication_coverage_and_reply_shapes() -> None:
    frozen = FrozenThreadSet(
        thread_ids=("t1", "t2"),
        snapshot_ref=artifact("t.json"),
        head_sha=SHA_A,
        cycle_number=1,
        trigger_marker="m",
    )
    with pytest.raises(ValidationError):
        AdjudicationEvidence(
            frozen=frozen,
            decisions=(
                ThreadDecisionRecord(
                    thread_id="t1",
                    decision=AdjudicationDecisionKind.ACTIONABLE,
                    safe_summary="a",
                ),
            ),
            result_ref=artifact("r.json"),
            fix_prompt_ref=artifact("f.txt"),
        )
    with pytest.raises(ValidationError):
        AdjudicationEvidence(
            frozen=frozen,
            decisions=(
                ThreadDecisionRecord(
                    thread_id="t1",
                    decision=AdjudicationDecisionKind.NOT_APPLICABLE,
                    safe_summary="a",
                    reply_ref=artifact("reply-a.txt"),
                ),
                ThreadDecisionRecord(
                    thread_id="t2",
                    decision=AdjudicationDecisionKind.UNCERTAIN,
                    safe_summary="b",
                    reply_ref=artifact("reply-b.txt"),
                ),
            ),
            result_ref=artifact("r.json"),
            fix_prompt_ref=artifact("fix.txt"),  # forbidden on reply path
        )


def test_models_are_frozen_and_forbid_extras() -> None:
    ref = artifact("x.txt")
    with pytest.raises(ValidationError):
        ArtifactRef(relative_path="x.txt", sha256=HASH_1, extra=1)  # type: ignore[call-arg]
    with pytest.raises((TypeError, ValidationError)):
        ref.relative_path = "mutated.txt"  # type: ignore[misc]
    frozen = FrozenThreadSet(
        thread_ids=("t1",),
        snapshot_ref=ref,
        head_sha=SHA_A,
        cycle_number=1,
        trigger_marker="m",
    )
    with pytest.raises((TypeError, ValidationError)):
        frozen.thread_ids += ("t2",)  # type: ignore[misc]
    # Tuples themselves are immutable even when read from the model.
    with pytest.raises(TypeError):
        frozen.thread_ids[0] = "mutated"  # type: ignore[index]


def test_reason_vocabulary_covers_required_kinds() -> None:
    assert TransientErrorKind.HTTP_429.value == "http_429"
    assert TransientErrorKind.HTTP_500.value == "http_500"
    assert TransientErrorKind.OTHER_HTTP_5XX.value == "other_http_5xx"
    assert PauseReasonKind.AMBIGUOUS_WRITE_UNRESOLVED.value == "ambiguous_write_unresolved"
    assert PauseReasonKind.NOT_FOUND.value == "not_found"
    assert PauseReasonKind.BRANCH_DRIFT.value == "branch_drift"
    assert FailureReasonKind.HASH_MISMATCH.value == "hash_mismatch"
    ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout")
    ErrorSummary(kind=TransientErrorKind.HTTP_500, safe_summary="server")
    SafeAction(kind=SafeActionKind.RESUME_SAME_EFFECT, condition="resume")


def test_effect_identity_stable_across_retries() -> None:
    a = build_effect_identity(run_id="r1", cycle_number=2, operation="push_commit", target="abc")
    b = build_effect_identity(run_id="r1", cycle_number=2, operation="push_commit", target="abc")
    assert a == b
    id1, key1 = stable_effect_ids(run_id="r1", cycle_number=2, operation="request_bot_review")
    id2, key2 = stable_effect_ids(run_id="r1", cycle_number=2, operation="request_bot_review")
    assert id1 == id2 == key1 == key2


def test_effect_classification_and_reconcile_cannot_embed_write_identity(
    repo: RepositoryIdentity,
) -> None:
    binding = PullRequestBinding(
        repository=repo,
        pr_number=1,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    request = RequestBotReviewEffect(
        effect_id="e-req",
        idempotency_key="e-req",
        run_id="r1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=repo,
        bound_head_sha=SHA_A,
        binding=binding,
        marker="marker",
    )
    observe = ObserveBotReviewEffect(
        effect_id="e-obs",
        idempotency_key="e-obs",
        run_id="r1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=repo,
        bound_head_sha=SHA_A,
        binding=binding,
        poll_sequence=1,
        trigger_marker="marker",
    )
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
    assert is_mutating_effect(request)
    assert is_read_only_effect(observe)
    assert classify_effect(observe).value == "read_only"
    reconcile = ReconcileWriteEffect(
        effect_id="e-rec",
        idempotency_key="e-rec",
        run_id="r1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=repo,
        bound_head_sha=SHA_A,
        original_write=commit,
        strategy="find_commit_at_head",
        reconciliation_identity="rec-1",
    )
    assert is_reconciling_effect(reconcile)
    with pytest.raises(ValidationError):
        ReconcileWriteEffect(
            effect_id="e-commit",
            idempotency_key="e-rec",
            run_id="r1",
            cycle_number=1,
            attempt=1,
            max_attempts=6,
            repository=repo,
            bound_head_sha=SHA_A,
            original_write=commit,
            strategy="find_commit_at_head",
            reconciliation_identity="rec-1",
        )

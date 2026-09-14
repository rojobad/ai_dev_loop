"""Unit tests for Phase 20.3 checkpoint-pending run and sequence states."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.scheduler.helpers import sample_submitted_state

from ai_dev_loop.scheduler.domain.events import (
    RunCompletedEvent,
    SequenceCheckpointRequestedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_run_completed,
    apply_sequence_checkpoint_requested,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    CheckpointPendingState,
    CodexWorkflowCheckpoint,
    CursorWorkflowCheckpoint,
)


def _awaiting_sequence_state() -> AwaitingCodexReviewState:
    base = sample_submitted_state(repo_root="/tmp/repo")
    context = base.context.model_copy(
        update={
            "schema_version": 4,
            "sequence": {
                "sequence_id": "seq-1",
                "ordinal": 1,
                "total_phases": 2,
                "entry_hash": "a" * 64,
            },
        }
    )
    return AwaitingCodexReviewState(
        run_id=base.run_id,
        version=3,
        submitted_at=base.submitted_at,
        updated_at=base.updated_at,
        idempotency_key=base.idempotency_key,
        context=context,
        checkpoint={
            "authorized_at": base.submitted_at,
            "admitted_at": base.submitted_at,
            "admission_status_artifact_path": "git/status/01-admission.txt",
            "admission_status_sha256": "b" * 64,
        },
        cursor=CursorWorkflowCheckpoint(
            staged_patch_path="git/diffs/01.patch",
            staged_patch_sha256="c" * 64,
        ),
        codex=CodexWorkflowCheckpoint(
            reviewer_session_id="019def00-0000-0000-0000-0000000000bb",
            latest_review_result_path="codex/reviews/01.json",
            latest_review_result_sha256="d" * 64,
        ),
    )


def test_non_final_sequence_accepted_review_enters_checkpoint_pending() -> None:
    state = _awaiting_sequence_state()
    event = SequenceCheckpointRequestedEvent(
        run_id=state.run_id,
        sequence_id="seq-1",
        accepted_outcome="completed",
        review_iteration=1,
        checkpoint_intent_artifact_path="sequence-checkpoints/intent.json",
        checkpoint_intent_sha256="e" * 64,
        checkpoint_trusted_tree_sha256="f" * 64,
    )
    pending = apply_sequence_checkpoint_requested(
        state,
        event,
        now_text=datetime(2026, 9, 13, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    assert isinstance(pending, CheckpointPendingState)
    assert pending.accepted_outcome == "completed"
    assert pending.codex.reviews_completed == 1


def test_checkpoint_pending_to_completed_preserves_review_counter() -> None:
    state = _awaiting_sequence_state()
    event = SequenceCheckpointRequestedEvent(
        run_id=state.run_id,
        sequence_id="seq-1",
        accepted_outcome="completed_with_residual_risk",
        review_iteration=1,
        checkpoint_intent_artifact_path="sequence-checkpoints/intent.json",
        checkpoint_intent_sha256="e" * 64,
        checkpoint_trusted_tree_sha256="f" * 64,
    )
    pending = apply_sequence_checkpoint_requested(
        state.model_copy(
            update={
                "codex": state.codex.model_copy(
                    update={
                        "latest_review_result_path": "codex/reviews/01.json",
                        "latest_review_result_sha256": "d" * 64,
                    }
                )
            }
        ),
        event,
        now_text=datetime(2026, 9, 13, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    completed = apply_run_completed(
        pending,
        RunCompletedEvent(run_id=pending.run_id, review_iteration=1),
        now_text=datetime(2026, 9, 13, 12, 1, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    assert completed.codex.reviews_completed == 1

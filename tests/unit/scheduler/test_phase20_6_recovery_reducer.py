"""Reducer tests for Phase 20.6 fresh-recovery correction chat binding."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_submitted_state

from ai_dev_loop.scheduler.domain.events import (
    CodexReviewerBoundEvent,
    CursorChatCreatedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_codex_reviewer_bound,
    apply_cursor_chat_bound_on_waiting_fix,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    AwaitingCodexReviewState,
    CodexWorkflowCheckpoint,
    CursorWorkflowCheckpoint,
    FreshReviewRecoveryLineage,
    WaitingForCursorFixState,
)


def test_apply_codex_reviewer_bound_preserves_fresh_recovery() -> None:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    submitted = sample_submitted_state(run_id="run-recovery")
    lineage = FreshReviewRecoveryLineage(
        recovery_id="rcv-test",
        source_run_id="run-source",
        source_staged_patch_sha256="a" * 64,
        source_parent_head="b" * 40,
        created_at=now_text,
    )
    state = AwaitingCodexReviewState(
        run_id=submitted.run_id,
        version=2,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=submitted.idempotency_key,
        context=submitted.context,
        checkpoint=AdmittedRunCheckpoint(
            authorized_at=now_text,
            authorized_controller_session_id=CONTROLLER_SESSION,
            admitted_at=now_text,
            admission_status_artifact_path="recovery/admission.json",
            admission_status_sha256="c" * 64,
        ),
        cursor=CursorWorkflowCheckpoint(
            iteration=1,
            staged_patch_path="recovery/source-staged.patch",
            staged_patch_sha256="a" * 64,
        ),
        fresh_recovery=lineage,
    )
    event = CodexReviewerBoundEvent(
        run_id=state.run_id,
        reviewer_session_id_prefix="019abc00",
        binding_artifact_path="codex/fresh-reviewer-binding.json",
        binding_artifact_sha256="d" * 64,
    )
    updated = apply_codex_reviewer_bound(
        state,
        event,
        now_text=now_text,
        reviewer_session_id="019abc00-0000-0000-0000-000000000000",
    )
    assert updated.fresh_recovery == lineage
    assert updated.codex.reviewer_session_id == "019abc00-0000-0000-0000-000000000000"


def test_apply_cursor_chat_bound_on_waiting_fix_preserves_fresh_recovery() -> None:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    submitted = sample_submitted_state(run_id="run-recovery")
    lineage = FreshReviewRecoveryLineage(
        recovery_id="rcv-test",
        source_run_id="run-source",
        source_staged_patch_sha256="a" * 64,
        source_parent_head="b" * 40,
        created_at=now_text,
    )
    state = WaitingForCursorFixState(
        run_id=submitted.run_id,
        version=3,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=submitted.idempotency_key,
        context=submitted.context,
        checkpoint=AdmittedRunCheckpoint(
            authorized_at=now_text,
            authorized_controller_session_id=CONTROLLER_SESSION,
            admitted_at=now_text,
            admission_status_artifact_path="recovery/admission.json",
            admission_status_sha256="c" * 64,
        ),
        cursor=CursorWorkflowCheckpoint(iteration=2),
        codex=CodexWorkflowCheckpoint(
            review_iteration=1,
            reviews_completed=1,
            reviewer_session_id="019abc00-0000-0000-0000-000000000000",
            latest_correction_envelope_path="prompts/fixes/01.execution-envelope.txt",
            latest_correction_envelope_sha256="d" * 64,
            latest_fix_prompt_path="prompts/fixes/01.txt",
            latest_fix_prompt_sha256="e" * 64,
        ),
        fresh_recovery=lineage,
    )
    event = CursorChatCreatedEvent(
        run_id=state.run_id,
        chat_id="00000000-0000-4000-8000-000000000123",
        chat_artifact_path="cursor/chat.json",
        chat_artifact_sha256="0" * 64,
    )
    updated = apply_cursor_chat_bound_on_waiting_fix(state, event, now_text=now_text)
    assert updated.cursor.chat_id == "00000000-0000-4000-8000-000000000123"
    assert updated.fresh_recovery == lineage
    assert updated.codex.latest_correction_envelope_path == state.codex.latest_correction_envelope_path
    assert updated.cursor.iteration == 2

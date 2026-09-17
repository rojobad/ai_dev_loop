"""Shared helpers for Phase 20.9 scheduler tests."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    AuthorizedState,
    CodexWorkflowCheckpoint,
    CompletedState,
    CursorWorkflowCheckpoint,
)

_REVIEWER_SESSION = "019abc00-0000-0000-0000-000000000000"


def completed_state_from_authorized(
    state: AuthorizedState,
    *,
    version: int,
    updated_at: str,
) -> CompletedState:
    checkpoint = AdmittedRunCheckpoint(
        authorized_at=state.authorized_at,
        authorized_controller_session_id=state.authorized_controller_session_id,
        admitted_at=state.authorized_at,
        admission_status_artifact_path="git/admission-status.txt",
        admission_status_sha256="a" * 64,
    )
    return CompletedState(
        run_id=state.run_id,
        version=version,
        submitted_at=state.submitted_at,
        updated_at=updated_at,
        idempotency_key=state.idempotency_key,
        context=state.context,
        checkpoint=checkpoint,
        cursor=CursorWorkflowCheckpoint(iteration=1),
        codex=CodexWorkflowCheckpoint(
            review_iteration=1,
            reviews_completed=1,
            reviewer_session_id=_REVIEWER_SESSION,
        ),
    )

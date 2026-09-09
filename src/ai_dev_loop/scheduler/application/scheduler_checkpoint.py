"""Shared scheduler admission checkpoint helpers."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.state import AdmittedRunCheckpoint, AdmittedState


def checkpoint_from_state(state: object) -> AdmittedRunCheckpoint | None:
    checkpoint = getattr(state, "checkpoint", None)
    if isinstance(checkpoint, AdmittedRunCheckpoint):
        return checkpoint
    if isinstance(state, AdmittedState):
        return AdmittedRunCheckpoint(
            authorized_at=state.authorized_at,
            authorized_controller_session_id=state.authorized_controller_session_id,
            admitted_at=state.admitted_at,
            admission_status_artifact_path=state.admission_status_artifact_path,
            admission_status_sha256=state.admission_status_sha256,
        )
    return None

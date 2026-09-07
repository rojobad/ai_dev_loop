"""Typed application contracts for the central scheduler."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.scheduler.domain.state import FreshCodexReviewerBinding, SubmittedRunContext


class AppModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchedulerEngineErrorKind(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    BUSY = "busy"
    SCHEMA = "schema"
    CORRUPTION = "corruption"
    INTERNAL = "internal"


class SchedulerEngineError(AiDevLoopError):
    def __init__(self, kind: SchedulerEngineErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        if kind in {SchedulerEngineErrorKind.VALIDATION, SchedulerEngineErrorKind.SCHEMA}:
            self.exit_code = ValidationError.exit_code


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"


class SafeNextActionKind(StrEnum):
    SCHEDULER_START = "scheduler_start"
    NONE = "none"


class SafeNextAction(AppModel):
    kind: SafeNextActionKind
    command: str | None = None


class SubmitResult(AppModel):
    run_id: str
    project_name: str
    state_kind: str
    reused_existing: bool
    safe_next_action: SafeNextAction


class SchedulerRunSummary(AppModel):
    run_id: str
    state_kind: str
    project_name: str
    repository_root: str
    controller_session_id_prefix: str
    reviewer_session_id_prefix: str | None
    submitted_at: str
    updated_at: str
    safe_next_action: SafeNextAction


class SchedulerStatusResult(AppModel):
    summary: SchedulerRunSummary
    idempotency_key_prefix: str
    worktree_key_prefix: str


def queued_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_START,
        command=f"ai_dev_loop scheduler start {run_id}",
    )


def redacted_session_prefix(session_id: str) -> str:
    if len(session_id) <= 12:
        return session_id
    return f"{session_id[:8]}…{session_id[-4:]}"


def summary_from_context(
    *,
    run_id: str,
    state_kind: str,
    submitted_at: str,
    updated_at: str,
    context: SubmittedRunContext,
    safe_next_action: SafeNextAction,
) -> SchedulerRunSummary:
    reviewer_prefix = None
    if isinstance(context.codex, FreshCodexReviewerBinding):
        reviewer_prefix = None
    else:
        reviewer_prefix = redacted_session_prefix(context.codex.session_id)
    return SchedulerRunSummary(
        run_id=run_id,
        state_kind=state_kind,
        project_name=context.project_name,
        repository_root=context.repository.root,
        controller_session_id_prefix=redacted_session_prefix(
            context.controller.controller_session_id
        ),
        reviewer_session_id_prefix=reviewer_prefix,
        submitted_at=submitted_at,
        updated_at=updated_at,
        safe_next_action=safe_next_action,
    )

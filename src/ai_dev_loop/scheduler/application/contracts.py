"""Typed application contracts for the central scheduler."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.scheduler.domain.state import (
    CodexRuntimeBinding,
    SchedulerState,
    SubmittedRunContext,
)


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
    SCHEDULER_TICK = "scheduler_tick"
    SCHEDULER_ABORT = "scheduler_abort"
    WAIT_UNTIL = "wait_until"
    INSPECT_BLOCKED = "inspect_blocked"
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
    cursor_wait_until: str | None = None
    block_reason_kind: str | None = None


class SchedulerStatusResult(AppModel):
    summary: SchedulerRunSummary
    idempotency_key_prefix: str
    worktree_key_prefix: str
    capacity_holder_run_id: str | None = None
    last_event_kind: str | None = None


class StartResult(AppModel):
    run_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


class TickRunReceipt(AppModel):
    run_id: str
    action: str
    detail: str | None = None


class TickReceipt(AppModel):
    tick_owner_id: str
    lease_generation: int
    visited_runs: int
    run_receipts: tuple[TickRunReceipt, ...]
    lease_acquired: bool
    safe_next_action: SafeNextAction


class AbortProcessAction(StrEnum):
    NONE = "none"
    TERMINATED = "terminated"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


class AbortResult(AppModel):
    run_id: str
    state_kind: str
    abort_persisted: bool
    idempotent_replay: bool
    process_action: AbortProcessAction
    termination_pending: bool = False
    safe_next_action: SafeNextAction


HARD_HISTORY_MAX = 200


class HistoryEntry(AppModel):
    sequence: int
    event_kind: str
    created_at: str
    safe_detail: str


class HistoryResult(AppModel):
    run_id: str
    order: str
    limit: int
    truncated: bool
    entries: tuple[HistoryEntry, ...]


class ControllerSchedulerCandidate(AppModel):
    run_id: str
    state_kind: str
    project_name: str
    repository_root: str
    submitted_at: str
    updated_at: str
    safe_next_action: SafeNextAction
    capacity_holder_run_id: str | None
    last_event_kind: str | None
    reviewer_session_id_prefix: str | None = None
    cursor_wait_until: str | None = None
    block_reason_kind: str | None = None


def queued_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_START,
        command=(
            f"ai_dev_loop scheduler start {run_id} --controller-session-id <exact-controller-id>"
        ),
    )


def authorized_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command="ai_dev_loop scheduler tick",
    )


def admitted_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command="ai_dev_loop scheduler tick",
    )


def active_cursor_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command="ai_dev_loop scheduler tick",
    )


def awaiting_codex_review_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command="ai_dev_loop scheduler tick",
    )


def waiting_for_cursor_fix_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command="ai_dev_loop scheduler tick",
    )


def terminal_review_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=None,
    )


def aborted_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=None,
    )


def aborted_pending_termination_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_ABORT,
        command=(
            f"Scheduler abort termination is still pending for run {run_id}. "
            f"Retry with ai_dev_loop scheduler abort {run_id} after verifying the "
            "owned unit is inactive. Do not relaunch or advance the run."
        ),
    )


def aborted_pending_resource_cleanup_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            f"Aborted run {run_id} still holds scheduler capacity or reservation. "
            "Run ai_dev_loop scheduler tick to finalize cleanup. "
            "Do not relaunch or advance the run."
        ),
    )


def blocked_safe_next_action(
    run_id: str, *, block_reason_kind: str | None = None
) -> SafeNextAction:
    detail = block_reason_kind or "blocked"
    return SafeNextAction(
        kind=SafeNextActionKind.INSPECT_BLOCKED,
        command=(
            f"Inspect protected scheduler artifacts for run {run_id} "
            f"(block_reason_kind={detail}). No automatic retry is available."
        ),
    )


def waiting_usage_limit_safe_next_action(wait_until: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.WAIT_UNTIL,
        command=(
            f"Wait until {wait_until}, then run ai_dev_loop scheduler tick "
            "(verified usage-limit retry only)."
        ),
    )


def scheduler_abort_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_ABORT,
        command=f"ai_dev_loop scheduler abort {run_id}",
    )


def safe_next_action_for_state_kind(
    state_kind: str,
    run_id: str,
    *,
    cursor_wait_until: str | None = None,
    block_reason_kind: str | None = None,
) -> SafeNextAction:
    if state_kind == "queued":
        return queued_safe_next_action(run_id)
    if state_kind == "authorized":
        return authorized_safe_next_action()
    if state_kind in {
        "admitted",
        "preflight_complete",
        "cursor_ready",
    }:
        return active_cursor_safe_next_action()
    if state_kind == "waiting_usage_limit":
        if cursor_wait_until:
            return waiting_usage_limit_safe_next_action(cursor_wait_until)
        return active_cursor_safe_next_action()
    if state_kind == "awaiting_codex_review":
        return awaiting_codex_review_safe_next_action()
    if state_kind == "waiting_for_cursor_fix":
        return waiting_for_cursor_fix_safe_next_action()
    if state_kind in {"completed", "completed_with_residual_risk", "max_iterations_reached"}:
        return terminal_review_safe_next_action()
    if state_kind == "aborted":
        return aborted_safe_next_action()
    if state_kind == "blocked":
        return blocked_safe_next_action(run_id, block_reason_kind=block_reason_kind)
    return blocked_safe_next_action(run_id)


def redacted_session_prefix(session_id: str) -> str:
    if len(session_id) <= 12:
        return session_id
    return f"{session_id[:8]}…{session_id[-4:]}"


def bound_reviewer_session_id_from_state(state: SchedulerState) -> str | None:
    codex = getattr(state, "codex", None)
    if codex is None:
        return None
    reviewer_session_id = getattr(codex, "reviewer_session_id", None)
    if reviewer_session_id is None:
        return None
    return str(reviewer_session_id)


def reviewer_session_id_prefix_for_projection(
    context: SubmittedRunContext,
    *,
    bound_reviewer_session_id: str | None = None,
) -> str | None:
    if isinstance(context.codex, CodexRuntimeBinding):
        return redacted_session_prefix(context.codex.session_id)
    if bound_reviewer_session_id:
        return redacted_session_prefix(bound_reviewer_session_id)
    return None


def scheduler_status_projection_from_state(state: object) -> dict[str, str | None]:
    from ai_dev_loop.scheduler.domain.state import BlockedState, WaitingUsageLimitState

    projection: dict[str, str | None] = {"cursor_wait_until": None, "block_reason_kind": None}
    if isinstance(state, WaitingUsageLimitState):
        projection["cursor_wait_until"] = state.cursor.wait_until
    if isinstance(state, BlockedState):
        projection["block_reason_kind"] = state.block_reason_kind
    return projection


def summary_from_context(
    *,
    run_id: str,
    state_kind: str,
    submitted_at: str,
    updated_at: str,
    context: SubmittedRunContext,
    safe_next_action: SafeNextAction | None = None,
    bound_reviewer_session_id: str | None = None,
    cursor_wait_until: str | None = None,
    block_reason_kind: str | None = None,
) -> SchedulerRunSummary:
    reviewer_prefix = reviewer_session_id_prefix_for_projection(
        context,
        bound_reviewer_session_id=bound_reviewer_session_id,
    )
    action = safe_next_action or safe_next_action_for_state_kind(
        state_kind,
        run_id,
        cursor_wait_until=cursor_wait_until,
        block_reason_kind=block_reason_kind,
    )
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
        safe_next_action=action,
        cursor_wait_until=cursor_wait_until,
        block_reason_kind=block_reason_kind,
    )

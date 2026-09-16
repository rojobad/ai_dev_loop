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
    controller_session_id_prefix: str | None
    reviewer_session_id_prefix: str | None
    review_iterations_completed: int
    max_review_iterations: int
    submitted_max_review_iterations: int | None = None
    submitted_at: str
    updated_at: str
    safe_next_action: SafeNextAction
    cursor_wait_until: str | None = None
    block_reason_kind: str | None = None
    sequence_id_prefix: str | None = None
    sequence_ordinal: int | None = None
    sequence_total_phases: int | None = None


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
DEFAULT_TIMELINE_LIMIT = 50
HARD_TIMELINE_MAX = 200

REVIEW_COMPLETION_EVENT_KINDS = (
    "waiting_for_cursor_fix_entered",
    "run_completed",
    "run_completed_with_residual_risk",
    "max_iterations_reached",
)


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


class TimelineEntry(AppModel):
    iteration: int
    phase: str
    phase_attempt: int
    status: str
    launch_requested_at: str | None
    completed_at: str | None
    observed_duration_seconds: float | None


class TimelineResult(AppModel):
    run_id: str
    order: str
    limit: int
    truncated: bool
    entries: tuple[TimelineEntry, ...]


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
    controller_session_id_prefix: str | None = None
    review_iterations_completed: int = 0
    max_review_iterations: int = 0
    cursor_wait_until: str | None = None
    block_reason_kind: str | None = None


def queued_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_START,
        command=f"ai_dev_loop scheduler start {run_id}",
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


def waiting_codex_review_retry_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            f"ai_dev_loop scheduler review retry {run_id} "
            "(authorize Codex review retry; tick launches the attempt)."
        ),
    )


def waiting_codex_capacity_safe_next_action(run_id: str | None = None) -> SafeNextAction:
    retry_hint = ""
    if run_id:
        retry_hint = (
            f" Or run `ai_dev_loop scheduler review retry {run_id}` to authorize "
            "a same-reviewer retry when capacity is available."
        )
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            "ai_dev_loop scheduler tick "
            "(waiting for Codex account capacity; no reset timer is scheduled)."
            f"{retry_hint}"
        ),
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


def max_iterations_reached_safe_next_action(run_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=(
            f"ai_dev_loop scheduler extend {run_id} "
            "--max-review-iterations <higher-total> "
            "(authorize a higher review ceiling; tick schedules the correction)."
        ),
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
    if state_kind == "waiting_codex_capacity":
        return waiting_codex_capacity_safe_next_action(run_id)
    if state_kind == "waiting_codex_review_retry":
        return waiting_codex_review_retry_safe_next_action(run_id)
    if state_kind == "awaiting_codex_review":
        return awaiting_codex_review_safe_next_action()
    if state_kind == "waiting_for_cursor_fix":
        return waiting_for_cursor_fix_safe_next_action()
    if state_kind == "checkpoint_pending":
        return checkpoint_pending_safe_next_action()
    if state_kind == "max_iterations_reached":
        return max_iterations_reached_safe_next_action(run_id)
    if state_kind in {"completed", "completed_with_residual_risk"}:
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


def redacted_controller_prefix(controller_session_id: str | None) -> str | None:
    if controller_session_id is None:
        return None
    return redacted_session_prefix(controller_session_id)


def review_budget_from_state(
    state: SchedulerState,
    *,
    ledger_reviews_completed: int | None = None,
    effective_max_review_iterations: int | None = None,
) -> tuple[int, int]:
    submitted_max = state.context.workflow.max_review_iterations
    max_reviews = (
        effective_max_review_iterations
        if effective_max_review_iterations is not None
        else submitted_max
    )
    codex = getattr(state, "codex", None)
    if codex is not None:
        return int(getattr(codex, "reviews_completed", 0) or 0), max_reviews
    if ledger_reviews_completed is not None:
        return ledger_reviews_completed, max_reviews
    return 0, max_reviews


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
    review_iterations_completed: int | None = None,
    max_review_iterations: int | None = None,
    submitted_max_review_iterations: int | None = None,
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
    sequence_binding = context.sequence
    return SchedulerRunSummary(
        run_id=run_id,
        state_kind=state_kind,
        project_name=context.project_name,
        repository_root=context.repository.root,
        controller_session_id_prefix=redacted_controller_prefix(
            context.controller.controller_session_id
        ),
        reviewer_session_id_prefix=reviewer_prefix,
        review_iterations_completed=(
            review_iterations_completed if review_iterations_completed is not None else 0
        ),
        max_review_iterations=(
            max_review_iterations
            if max_review_iterations is not None
            else context.workflow.max_review_iterations
        ),
        submitted_max_review_iterations=submitted_max_review_iterations,
        submitted_at=submitted_at,
        updated_at=updated_at,
        safe_next_action=action,
        cursor_wait_until=cursor_wait_until,
        block_reason_kind=block_reason_kind,
        sequence_id_prefix=(
            sequence_binding.sequence_id[:8] if sequence_binding is not None else None
        ),
        sequence_ordinal=sequence_binding.ordinal if sequence_binding is not None else None,
        sequence_total_phases=(
            sequence_binding.total_phases if sequence_binding is not None else None
        ),
    )


class SequenceEntrySummary(AppModel):
    ordinal: int
    phase_name: str
    planned_run_id_prefix: str
    commit_message_present: bool
    materialized: bool = False
    materialized_run_id_prefix: str | None = None
    accepted_outcome: str | None = None
    residual_risk: bool = False
    checkpoint_commit_sha256_prefix: str | None = None
    cancelled: bool = False


class SequenceAggregateCounts(AppModel):
    planned: int
    materialized: int
    accepted: int
    residual_risk: int
    checkpointed: int
    cancelled: int
    remaining: int


class SequencePrepareResult(AppModel):
    sequence_id: str
    name: str
    state_kind: str
    entry_count: int
    reused_existing: bool
    safe_next_action: SafeNextAction


class SequenceStatusResult(AppModel):
    sequence_id: str
    name: str
    state_kind: str
    project_name: str
    repository_root: str
    entry_count: int
    current_ordinal: int | None
    current_run_id: str | None = None
    current_run_state_kind: str | None = None
    current_phase_name: str | None = None
    residual_risk: bool | None = None
    residual_risk_ordinals: tuple[int, ...] = ()
    block_reason_kind: str | None = None
    abort_reason: str | None = None
    finalized_at: str | None = None
    completion_report_sha256_prefix: str | None = None
    prepared_at: str
    updated_at: str
    started_at: str | None = None
    idempotency_key_prefix: str
    entries: tuple[SequenceEntrySummary, ...]
    aggregate_counts: SequenceAggregateCounts | None = None
    safe_next_action: SafeNextAction


class SequenceAbortResult(AppModel):
    sequence_id: str
    state_kind: str
    abort_persisted: bool
    idempotent_replay: bool
    run_abort_process_action: AbortProcessAction
    run_termination_pending: bool = False
    safe_next_action: SafeNextAction


class SequenceStartResult(AppModel):
    sequence_id: str
    run_id: str
    sequence_state_kind: str
    run_state_kind: str
    current_ordinal: int
    entry_count: int
    current_phase_name: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


def prepared_sequence_start_next_action(sequence_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_START,
        command=f"ai_dev_loop scheduler sequence start {sequence_id}",
    )


def prepared_sequence_safe_next_action(sequence_id: str) -> SafeNextAction:
    return prepared_sequence_start_next_action(sequence_id)


SEQUENCE_CHECKPOINT_BOUNDARY_RUN_STATE_KINDS = frozenset(
    {
        "completed",
        "completed_with_residual_risk",
    }
)


def sequence_exposes_checkpoint_boundary(
    *,
    run_state_kind: str,
    current_ordinal: int,
    total_phases: int,
) -> bool:
    return (
        run_state_kind in SEQUENCE_CHECKPOINT_BOUNDARY_RUN_STATE_KINDS
        and current_ordinal < total_phases
    )


def active_sequence_safe_next_action(
    *,
    sequence_id: str,
    run_safe_action: SafeNextAction,
) -> SafeNextAction:
    if run_safe_action.kind is SafeNextActionKind.SCHEDULER_TICK:
        return SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_TICK,
            command=(
                f"Active sequence {sequence_id} is waiting on materialized run progress. "
                f"{run_safe_action.command}"
            ),
        )
    return run_safe_action


def checkpoint_pending_safe_next_action() -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            "ai_dev_loop scheduler tick "
            "(sequence checkpoint reconciliation and handoff in progress)."
        ),
    )


def awaiting_finalization_sequence_safe_next_action(sequence_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.INSPECT_BLOCKED,
        command=(
            f"Sequence {sequence_id} is awaiting finalization. Inspect staged final-phase "
            f"changes with ai_dev_loop scheduler sequence status {sequence_id}. "
            "Final commit, push, and PR remain operator actions."
        ),
    )


def recovery_integrated_finalization_sequence_safe_next_action(sequence_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=(
            f"Sequence {sequence_id} completed via authenticated recovery integration. "
            "Push, PR review, and merge remain manual operator actions."
        ),
    )


def blocked_sequence_safe_next_action(
    sequence_id: str, *, block_reason_kind: str | None = None
) -> SafeNextAction:
    detail = block_reason_kind or "blocked"
    return SafeNextAction(
        kind=SafeNextActionKind.INSPECT_BLOCKED,
        command=(
            f"Sequence {sequence_id} is blocked ({detail}). Inspect materialized runs and "
            "protected artifacts. No automatic retry or successor materialization is available."
        ),
    )


def aborted_sequence_safe_next_action(sequence_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=(
            f"Sequence {sequence_id} was aborted. Later planned phases were cancelled without "
            "creating scheduler runs."
        ),
    )


class RecoveryPrepareResult(AppModel):
    recovery_id: str
    source_run_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


class RecoveryStartResult(AppModel):
    recovery_id: str
    recovery_run_id: str
    source_run_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


class RecoveryStatusResult(AppModel):
    recovery_id: str
    recovery_id_prefix: str
    source_run_id_prefix: str
    state_kind: str
    recovery_run_id_prefix: str | None = None
    sequence_id_prefix: str | None = None
    sequence_ordinal: int | None = None
    accepted_outcome: str | None = None
    residual_risk: bool | None = None
    integrated_commit_prefix: str | None = None
    safe_next_action: SafeNextAction


class RecoveryAbortResult(AppModel):
    recovery_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


def prepared_recovery_start_next_action(recovery_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_START,
        command=f"ai_dev_loop scheduler recovery start {recovery_id}",
    )


def active_recovery_tick_next_action(recovery_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            f"Fresh recovery {recovery_id} is active. "
            "Run ai_dev_loop scheduler tick to continue review and integration."
        ),
    )


def integrated_recovery_safe_next_action(recovery_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=(
            f"Recovery {recovery_id} integrated successfully. "
            "Push, PR review, and merge remain manual operator actions."
        ),
    )


class RolloverPrepareResult(AppModel):
    rollover_id: str
    source_run_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


class RolloverStartResult(AppModel):
    rollover_id: str
    rollover_run_id: str
    source_run_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


class RolloverStatusResult(AppModel):
    rollover_id: str
    rollover_id_prefix: str
    source_run_id_prefix: str
    state_kind: str
    rollover_run_id_prefix: str | None = None
    sequence_id_prefix: str | None = None
    sequence_ordinal: int | None = None
    accepted_outcome: str | None = None
    residual_risk: bool | None = None
    integrated_commit_prefix: str | None = None
    safe_next_action: SafeNextAction


class RolloverAbortResult(AppModel):
    rollover_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    safe_next_action: SafeNextAction


def prepared_rollover_start_next_action(rollover_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_START,
        command=f"ai_dev_loop scheduler rollover start {rollover_id}",
    )


def active_rollover_tick_next_action(rollover_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            f"Authenticated rollover {rollover_id} is active. "
            "Run ai_dev_loop scheduler tick to continue review and integration."
        ),
    )


def integrated_rollover_safe_next_action(rollover_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.NONE,
        command=(
            f"Rollover {rollover_id} integrated successfully. "
            "Push, PR review, and merge remain manual operator actions."
        ),
    )


def abort_pending_sequence_safe_next_action(sequence_id: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.SCHEDULER_TICK,
        command=(
            f"Sequence {sequence_id} abort is pending. Run ai_dev_loop scheduler tick to "
            "reconcile the active run abort."
        ),
    )

"""Resume checkpoint planning and artifact completion probes."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import (
    iteration_label,
    iteration_staged_patch_rel_path,
    local_review_budget_used,
    max_iteration_number,
    pending_external_cursor_iteration,
)
from ai_dev_loop.runners.codex import load_review_result_from_artifacts
from ai_dev_loop.runners.git import (
    git_status_porcelain,
    paths_with_unstaged_changes,
    paths_with_untracked,
    validate_staged_patch_matches_artifact,
)
from ai_dev_loop.runners.staging import staging_complete_for_iteration
from ai_dev_loop.state import RunState, RunStatus, format_utc_timestamp, transition_status, utc_now


class WorkflowActionKind(StrEnum):
    CURSOR = "cursor"
    STAGING = "staging"
    REVIEW = "review"
    PROCESS_REVIEW = "process_review"


@dataclass(frozen=True)
class WorkflowAction:
    kind: WorkflowActionKind
    iteration_number: int


@dataclass(frozen=True)
class InterruptedCheckpoint:
    status: RunStatus
    iteration_number: int


TERMINAL_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        RunStatus.MAX_ITERATIONS_REACHED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    }
)

CHECKPOINT_STATUSES_REQUIRING_CHAT_ID = frozenset(
    {
        RunStatus.WAITING_FOR_CURSOR_FIX,
        RunStatus.RUNNING_CURSOR,
        RunStatus.STAGING,
        RunStatus.REVIEWING,
        RunStatus.INTERRUPTED,
    }
)


def preserve_artifact_before_retry(path: Path) -> None:
    if not path.is_file():
        return
    timestamp = format_utc_timestamp(utc_now())
    preserved = path.with_name(f"{path.stem}.attempt-{timestamp}{path.suffix}")
    shutil.copy2(path, preserved)


def cursor_metadata_path(run_directory: Path, iteration_number: int) -> Path:
    return run_directory / f"cursor/iterations/{iteration_label(iteration_number)}/metadata.json"


def cursor_turn_complete(run_directory: Path, iteration_number: int) -> bool:
    metadata_path = cursor_metadata_path(run_directory, iteration_number)
    if not metadata_path.is_file():
        return False
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("exit_code") == 0 and not payload.get("timed_out")


def review_result_available(run_directory: Path, iteration_number: int) -> bool:
    result_path = run_directory / f"codex/reviews/{iteration_label(iteration_number)}.json"
    if not result_path.is_file():
        return False
    try:
        load_review_result_from_artifacts(run_directory, iteration_label(iteration_number))
    except ValidationError:
        return False
    return True


def has_workflow_progress(state: RunState, run_directory: Path) -> bool:
    if max_iteration_number(state) > 0:
        return True
    if (run_directory / "cursor" / "chat.json").is_file():
        return True
    return (run_directory / "cursor" / "iterations" / "01").exists()


def requires_persisted_cursor_chat_id(state: RunState, run_directory: Path) -> bool:
    if state.status in CHECKPOINT_STATUSES_REQUIRING_CHAT_ID:
        return True
    if state.status == RunStatus.VALIDATING and has_workflow_progress(state, run_directory):
        return True
    return has_workflow_progress(state, run_directory)


def validate_recorded_staged_patch_for_review(
    state: RunState,
    run_directory: Path,
    iteration_number: int,
) -> None:
    patch_rel = iteration_staged_patch_rel_path(state, iteration_number)
    patch_artifact = run_directory / patch_rel
    repo_root = Path(state.repository.root)
    validate_staged_patch_matches_artifact(repo_root, patch_artifact)
    status = git_status_porcelain(repo_root)
    unstaged = paths_with_unstaged_changes(status)
    if unstaged:
        joined = ", ".join(sorted(unstaged))
        raise ValidationError(f"unstaged tracked changes detected before review: {joined}")
    untracked = paths_with_untracked(status)
    if untracked:
        joined = ", ".join(sorted(untracked))
        raise ValidationError(f"untracked files detected before review: {joined}")


def derive_interrupted_checkpoint(state: RunState, run_directory: Path) -> InterruptedCheckpoint:
    action = _plan_from_interrupted(state, run_directory)
    if action.kind == WorkflowActionKind.CURSOR:
        return InterruptedCheckpoint(RunStatus.RUNNING_CURSOR, action.iteration_number)
    if action.kind == WorkflowActionKind.STAGING:
        return InterruptedCheckpoint(RunStatus.STAGING, action.iteration_number)
    return InterruptedCheckpoint(RunStatus.REVIEWING, action.iteration_number)


def restore_workflow_checkpoint(state: RunState, run_directory: Path) -> InterruptedCheckpoint:
    checkpoint = derive_interrupted_checkpoint(state, run_directory)
    transition_status(state.status, checkpoint.status)
    state.status = checkpoint.status
    if checkpoint.status in {RunStatus.STAGING, RunStatus.REVIEWING}:
        state.workflow.current_review_iteration = checkpoint.iteration_number
    return checkpoint


def restore_interrupted_checkpoint(state: RunState, run_directory: Path) -> InterruptedCheckpoint:
    if state.status != RunStatus.INTERRUPTED:
        raise ValidationError(
            f"restore_interrupted_checkpoint requires interrupted status, not {state.status.value}"
        )
    return restore_workflow_checkpoint(state, run_directory)


def active_cursor_iteration(state: RunState, run_directory: Path) -> int:
    pending_external = pending_external_cursor_iteration(state)
    if pending_external is not None and not cursor_turn_complete(run_directory, pending_external):
        return pending_external
    if (
        state.workflow.current_review_iteration > 0
        and state.status in {RunStatus.WAITING_FOR_CURSOR_FIX, RunStatus.RUNNING_CURSOR}
        and not cursor_turn_complete(run_directory, state.workflow.current_review_iteration + 1)
    ):
        return state.workflow.current_review_iteration + 1
    return max(max_iteration_number(state), 1)


def active_review_iteration(state: RunState, run_directory: Path) -> int:
    if state.workflow.current_review_iteration > 0:
        return state.workflow.current_review_iteration
    return max(max_iteration_number(state), 1)


def plan_next_action(state: RunState, run_directory: Path) -> WorkflowAction | None:
    if state.status in TERMINAL_STATUSES:
        return None

    if state.status == RunStatus.VALIDATING:
        if has_workflow_progress(state, run_directory):
            return _plan_from_interrupted(state, run_directory)
        return WorkflowAction(WorkflowActionKind.CURSOR, 1)

    if state.status == RunStatus.WAITING_FOR_CURSOR_FIX:
        if local_review_budget_used(state) >= state.workflow.max_review_iterations:
            raise ValidationError(
                "review iteration limit already reached; resume cannot send another Cursor correction"
            )
        return WorkflowAction(
            WorkflowActionKind.CURSOR,
            state.workflow.current_review_iteration + 1,
        )

    if state.status == RunStatus.RUNNING_CURSOR:
        iteration_number = active_cursor_iteration(state, run_directory)
        if not cursor_turn_complete(run_directory, iteration_number):
            return WorkflowAction(WorkflowActionKind.CURSOR, iteration_number)
        return WorkflowAction(WorkflowActionKind.STAGING, iteration_number)

    if state.status == RunStatus.STAGING:
        iteration_number = active_cursor_iteration(state, run_directory)
        label = iteration_label(iteration_number)
        if not staging_complete_for_iteration(state, run_directory, label):
            return WorkflowAction(WorkflowActionKind.STAGING, iteration_number)
        return WorkflowAction(WorkflowActionKind.REVIEW, iteration_number)

    if state.status == RunStatus.REVIEWING:
        iteration_number = active_review_iteration(state, run_directory)
        if review_result_available(run_directory, iteration_number):
            return WorkflowAction(WorkflowActionKind.PROCESS_REVIEW, iteration_number)
        return WorkflowAction(WorkflowActionKind.REVIEW, iteration_number)

    if state.status == RunStatus.INTERRUPTED:
        return _plan_from_interrupted(state, run_directory)

    raise ValidationError(
        f"cannot determine next safe action for status {state.status.value}; inspect artifacts manually"
    )


def _plan_from_interrupted(state: RunState, run_directory: Path) -> WorkflowAction:
    pending_external = pending_external_cursor_iteration(state)
    if pending_external is not None and not cursor_turn_complete(run_directory, pending_external):
        return WorkflowAction(WorkflowActionKind.CURSOR, pending_external)

    iteration_number = max(max_iteration_number(state), 1)
    label = iteration_label(iteration_number)

    if not cursor_turn_complete(run_directory, iteration_number):
        return WorkflowAction(WorkflowActionKind.CURSOR, iteration_number)

    if not staging_complete_for_iteration(state, run_directory, label):
        return WorkflowAction(WorkflowActionKind.STAGING, iteration_number)

    if review_result_available(run_directory, iteration_number):
        return WorkflowAction(WorkflowActionKind.PROCESS_REVIEW, iteration_number)

    return WorkflowAction(WorkflowActionKind.REVIEW, iteration_number)

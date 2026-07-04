"""Start-run preflight validation."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.state import RunState, RunStatus, sha256_file, transition_status


def validate_start_status(state: RunState) -> None:
    if state.status != RunStatus.PREPARED:
        raise ValidationError(
            f"run must be in prepared status to start; current status is {state.status.value}"
        )


def validate_timeouts(state: RunState) -> None:
    if state.workflow.cursor_timeout_minutes <= 0:
        raise ValidationError("cursor timeout must be positive")
    if state.workflow.codex_timeout_minutes <= 0:
        raise ValidationError("codex timeout must be positive")


def validate_codex_session(state: RunState) -> None:
    if not state.codex.session_id.strip():
        raise ValidationError("codex session id is missing from prepared state")


def validate_repository_contract(state: RunState, run_directory: Path) -> None:
    repo_root = Path(state.repository.root)
    if not repo_root.is_dir():
        raise ValidationError(f"repository root does not exist: {repo_root}")

    repo_info = discover_repository(repo_root)
    if repo_info.branch != state.repository.branch:
        raise ValidationError(
            f"repository branch changed: expected {state.repository.branch}, found {repo_info.branch}"
        )
    if repo_info.head != state.repository.initial_head:
        raise ValidationError("repository HEAD changed since prepare; create a new prepared run")


def validate_plan_contract(state: RunState, run_directory: Path) -> None:
    snapshot = run_directory / state.plan.snapshot_path
    if not snapshot.is_file():
        raise ValidationError(f"plan snapshot missing: {state.plan.snapshot_path}")
    if sha256_file(snapshot) != state.plan.sha256:
        raise ValidationError("plan snapshot hash does not match prepared state")

    repo_root = Path(state.repository.root)
    repo_plan = repo_root / state.plan.repository_path
    if not repo_plan.is_file():
        raise ValidationError(f"repository plan file missing: {state.plan.repository_path}")
    if sha256_file(repo_plan) != state.plan.sha256:
        raise ValidationError("repository plan file hash does not match prepared state")


def validate_prompt_contract(state: RunState, run_directory: Path) -> None:
    snapshot = run_directory / state.prompt.snapshot_path
    if not snapshot.is_file():
        raise ValidationError(f"prompt snapshot missing: {state.prompt.snapshot_path}")
    if sha256_file(snapshot) != state.prompt.sha256:
        raise ValidationError("prompt snapshot hash does not match prepared state")

    repo_root = Path(state.repository.root)
    source_path = repo_root / state.prompt.source_repository_path
    if source_path.is_file() and sha256_file(source_path) != state.prompt.sha256:
        raise ValidationError("prompt source file hash does not match prepared state")


def validate_worktree_baseline(state: RunState, run_directory: Path) -> None:
    baseline_path = run_directory / state.repository.baseline_status_path
    if not baseline_path.is_file():
        raise ValidationError(f"baseline status missing: {state.repository.baseline_status_path}")

    repo_root = Path(state.repository.root)
    current_status = discover_repository(repo_root).status_porcelain
    baseline_status = baseline_path.read_text(encoding="utf-8").rstrip("\n")
    if current_status != baseline_status:
        raise ValidationError("worktree status changed since prepare; unexpected changes detected")


def run_start_preflight_checks(state: RunState, run_directory: Path) -> None:
    validate_timeouts(state)
    validate_codex_session(state)
    validate_repository_contract(state, run_directory)
    validate_plan_contract(state, run_directory)
    validate_prompt_contract(state, run_directory)
    validate_worktree_baseline(state, run_directory)


def run_start_preflight(state: RunState, run_directory: Path) -> None:
    validate_start_status(state)
    begin_validating(state)
    run_start_preflight_checks(state, run_directory)


def begin_validating(state: RunState) -> None:
    transition_status(state.status, RunStatus.VALIDATING)
    state.status = RunStatus.VALIDATING


def begin_running_cursor(state: RunState) -> None:
    transition_status(state.status, RunStatus.RUNNING_CURSOR)
    state.status = RunStatus.RUNNING_CURSOR


def begin_staging(state: RunState) -> None:
    transition_status(state.status, RunStatus.STAGING)
    state.status = RunStatus.STAGING


def mark_failed(state: RunState, message: str) -> None:
    if state.status in {RunStatus.FAILED, RunStatus.ABORTED}:
        state.last_error = message
        return
    try:
        transition_status(state.status, RunStatus.FAILED)
    except ValueError:
        state.status = RunStatus.FAILED
    state.status = RunStatus.FAILED
    state.last_error = message


def mark_interrupted(state: RunState, message: str) -> None:
    try:
        transition_status(state.status, RunStatus.INTERRUPTED)
    except ValueError:
        state.status = RunStatus.INTERRUPTED
    state.status = RunStatus.INTERRUPTED
    state.last_error = message

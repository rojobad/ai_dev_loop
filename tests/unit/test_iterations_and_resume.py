"""Unit tests for iteration helpers and resume planner."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import (
    cursor_prompt_path,
    find_iteration,
    fix_prompt_path,
    iteration_kind,
    upsert_iteration,
)
from ai_dev_loop.resume_planner import (
    WorkflowActionKind,
    plan_next_action,
    requires_persisted_cursor_chat_id,
    restore_interrupted_checkpoint,
)
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    utc_now,
)


def _sample_state(*, status: RunStatus = RunStatus.PREPARED) -> RunState:
    now = utc_now()
    return RunState(
        run_id="fixture-project-20260704T134512Z-abc123",
        project=ProjectRef(name="fixture-project"),
        status=status,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="main",
            initial_head="abc123",
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256="a" * 64,
        ),
        prompt=PromptState(
            source_repository_path="docs/plans/prompt_sample-plan.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            session_model=None,
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
            chat_id="019abc00-1111-2222-3333-444444444444",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=0,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
    )


def test_iteration_helpers_do_not_overwrite_prior_entries() -> None:
    state = _sample_state()
    upsert_iteration(
        state,
        {
            "number": 1,
            "kind": "initial_implementation",
            "cursor": {"prompt_path": "prompts/cursor-initial.txt"},
        },
    )
    upsert_iteration(
        state,
        {
            "number": 2,
            "kind": "cursor_correction",
            "cursor": {"prompt_path": "prompts/fixes/01.txt"},
        },
    )
    assert len(state.iterations) == 2
    assert find_iteration(state, 1)["cursor"]["prompt_path"] == "prompts/cursor-initial.txt"
    assert cursor_prompt_path(state, 2) == "prompts/fixes/01.txt"
    assert fix_prompt_path(1) == "prompts/fixes/01.txt"
    assert iteration_kind(2) == "cursor_correction"


def test_plan_next_action_for_waiting_for_cursor_fix() -> None:
    state = _sample_state(status=RunStatus.WAITING_FOR_CURSOR_FIX)
    state.workflow.current_review_iteration = 1
    action = plan_next_action(state, Path("/tmp/run"))
    assert action is not None
    assert action.kind == WorkflowActionKind.CURSOR
    assert action.iteration_number == 2


def test_plan_next_action_refuses_waiting_when_max_iterations_reached() -> None:
    state = _sample_state(status=RunStatus.WAITING_FOR_CURSOR_FIX)
    state.workflow.current_review_iteration = 3
    state.workflow.max_review_iterations = 3
    with pytest.raises(ValidationError, match="review iteration limit already reached"):
        plan_next_action(state, Path("/tmp/run"))


def test_restore_interrupted_checkpoint_after_cursor_complete(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    iteration_dir = run_directory / "cursor" / "iterations" / "01"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "metadata.json").write_text(
        '{"exit_code": 0, "timed_out": false}',
        encoding="utf-8",
    )
    state = _sample_state(status=RunStatus.INTERRUPTED)
    checkpoint = restore_interrupted_checkpoint(state, run_directory)
    assert checkpoint.status == RunStatus.STAGING
    assert state.status == RunStatus.STAGING
    assert checkpoint.iteration_number == 1


def test_restore_interrupted_checkpoint_after_staging_complete(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    iteration_dir = run_directory / "cursor" / "iterations" / "01"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "metadata.json").write_text(
        '{"exit_code": 0, "timed_out": false}',
        encoding="utf-8",
    )
    patch_path = run_directory / "git" / "diffs" / "01.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text("diff\n", encoding="utf-8")
    state = _sample_state(status=RunStatus.INTERRUPTED)
    state.iterations = [
        {
            "number": 1,
            "kind": "initial_implementation",
            "git": {"staged_diff_path": "git/diffs/01.patch"},
        }
    ]
    checkpoint = restore_interrupted_checkpoint(state, run_directory)
    assert checkpoint.status == RunStatus.REVIEWING
    assert state.status == RunStatus.REVIEWING
    assert state.workflow.current_review_iteration == 1


def test_requires_persisted_cursor_chat_id_for_checkpointed_runs(tmp_path: Path) -> None:
    state = _sample_state(status=RunStatus.REVIEWING)
    assert requires_persisted_cursor_chat_id(state, tmp_path / "run") is True
    prepared = _sample_state(status=RunStatus.PREPARED)
    assert requires_persisted_cursor_chat_id(prepared, tmp_path / "run") is False


def test_plan_next_action_refuses_unknown_status(tmp_path: Path) -> None:
    state = _sample_state(status=RunStatus.FAILED)
    assert plan_next_action(state, tmp_path) is None


def test_status_transitions_include_max_iterations_and_resume_paths() -> None:
    from ai_dev_loop.state import RunStatus, transition_status

    transition_status(RunStatus.REVIEWING, RunStatus.MAX_ITERATIONS_REACHED)
    transition_status(RunStatus.WAITING_FOR_CURSOR_FIX, RunStatus.MAX_ITERATIONS_REACHED)
    transition_status(RunStatus.INTERRUPTED, RunStatus.VALIDATING)
    transition_status(RunStatus.INTERRUPTED, RunStatus.STAGING)
    transition_status(RunStatus.INTERRUPTED, RunStatus.REVIEWING)
    transition_status(RunStatus.VALIDATING, RunStatus.STAGING)
    transition_status(RunStatus.VALIDATING, RunStatus.REVIEWING)
    for terminal in (
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        RunStatus.MAX_ITERATIONS_REACHED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    ):
        with pytest.raises(ValueError):
            transition_status(terminal, RunStatus.VALIDATING)


def test_validate_resume_status_rejects_terminal() -> None:
    from ai_dev_loop.commands.start_preflight import validate_resume_status

    state = _sample_state(status=RunStatus.COMPLETED)
    with pytest.raises(ValidationError, match="terminal status"):
        validate_resume_status(state)

"""Unit tests for Phase 16.2 typed local review/fix contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.local_review_loop import (
    LocalReviewOutcome,
    ScheduledCursorTurn,
    outcome_from_status,
    validate_scheduled_cursor_turn,
)
from ai_dev_loop.resume_planner import LocalInvocationContext, WorkflowActionKind, plan_next_action
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


def _sample_state(*, status: RunStatus = RunStatus.RUNNING_CURSOR) -> RunState:
    now = utc_now()
    return RunState(
        run_id="fixture-project-20260720T000000Z-phase162",
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
            session_id="019abc00-0000-0000-0000-0000000000bb",
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
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        iterations=[
            {
                "number": 1,
                "kind": "initial_implementation",
                "cursor": {"prompt_path": "prompts/cursor-initial.txt", "exit_code": 0},
                "git": {"staged_diff_path": "git/diffs/01.patch"},
            }
        ],
    )


def test_outcome_from_status_maps_known_values() -> None:
    assert outcome_from_status("completed") is LocalReviewOutcome.ACCEPTED
    assert (
        outcome_from_status("completed_with_residual_risk")
        is LocalReviewOutcome.ACCEPTED_WITH_RESIDUAL_RISK
    )
    assert (
        outcome_from_status("max_iterations_reached") is LocalReviewOutcome.MAX_ITERATIONS_REACHED
    )
    assert outcome_from_status("unknown_status") is LocalReviewOutcome.OTHER


@pytest.mark.parametrize(
    "prompt_path",
    [
        "",
        "   ",
        "/abs/prompts/fix.txt",
        "../escape.txt",
        "prompts/../../etc/passwd",
    ],
)
def test_scheduled_turn_rejects_invalid_prompt_paths(prompt_path: str) -> None:
    turn = ScheduledCursorTurn(iteration_number=2, prompt_path=prompt_path)
    with pytest.raises(ValidationError):
        validate_scheduled_cursor_turn(turn)


def test_scheduled_turn_rejects_missing_empty_and_collision(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    prompts = run_directory / "prompts" / "fixes"
    prompts.mkdir(parents=True)
    state = _sample_state()

    missing = ScheduledCursorTurn(iteration_number=2, prompt_path="prompts/fixes/missing.txt")
    with pytest.raises(ValidationError, match="prompt missing"):
        validate_scheduled_cursor_turn(missing, run_directory=run_directory, state=state)

    empty = prompts / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    empty_turn = ScheduledCursorTurn(iteration_number=2, prompt_path="prompts/fixes/empty.txt")
    with pytest.raises(ValidationError, match="prompt is empty"):
        validate_scheduled_cursor_turn(empty_turn, run_directory=run_directory, state=state)

    good = prompts / "ok.txt"
    good.write_text("fix me\n", encoding="utf-8")
    iteration_dir = run_directory / "cursor" / "iterations" / "02"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "metadata.json").write_text(
        '{"exit_code": 0, "timed_out": false}',
        encoding="utf-8",
    )
    collide = ScheduledCursorTurn(iteration_number=2, prompt_path="prompts/fixes/ok.txt")
    with pytest.raises(ValidationError, match="collides with a completed iteration"):
        validate_scheduled_cursor_turn(collide, run_directory=run_directory, state=state)


def test_plan_next_action_uses_explicit_scheduled_turn(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    (run_directory / "cursor" / "iterations" / "01").mkdir(parents=True)
    (run_directory / "cursor" / "iterations" / "01" / "metadata.json").write_text(
        '{"exit_code": 0, "timed_out": false}',
        encoding="utf-8",
    )
    (run_directory / "prompts" / "fixes").mkdir(parents=True)
    (run_directory / "prompts" / "fixes" / "external.txt").write_text(
        "external\n", encoding="utf-8"
    )
    state = _sample_state(status=RunStatus.RUNNING_CURSOR)
    # Without GitHub state: only the explicit scheduled turn forces iteration 3.
    assert state.github_pr_review is None
    context = LocalInvocationContext(
        scheduled_first_cursor_turn=ScheduledCursorTurn(
            iteration_number=3,
            prompt_path="prompts/fixes/external.txt",
        )
    )
    action = plan_next_action(state, run_directory, context=context)
    assert action is not None
    assert action.kind == WorkflowActionKind.CURSOR
    assert action.iteration_number == 3


def test_local_modules_have_no_pr_github_publication_imports() -> None:
    import ast

    root = Path(__file__).resolve().parents[2] / "src" / "ai_dev_loop"
    forbidden_modules = {
        "ai_dev_loop.commands.pr_review",
        "ai_dev_loop.commands.pr_review_recover",
        "ai_dev_loop.commands.pr_review_independent",
        "ai_dev_loop.legacy_pr_review_local_adapter",
        "ai_dev_loop.pr_review_worker",
        "ai_dev_loop.runners.github",
        "ai_dev_loop.runners.publish",
        "ai_dev_loop.runners.codex_github",
        "ai_dev_loop.github_pr_review_result",
        "ai_dev_loop.external_adjudication",
    }
    modules = (
        "local_review_loop.py",
        "workflow_engine.py",
        "iterations.py",
        "resume_planner.py",
    )
    for name in modules:
        path = root / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, f"{name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module not in forbidden_modules, f"{name} imports from {module}"
                for alias in node.names:
                    assert alias.name != "github_pr_review", f"{name} imports github_pr_review"
                    assert alias.name != "GithubPrReviewState", (
                        f"{name} imports GithubPrReviewState"
                    )
        # Attribute access to github_pr_review on state must not appear.
        source = path.read_text(encoding="utf-8")
        assert "state.github_pr_review" not in source, f"{name} accesses state.github_pr_review"

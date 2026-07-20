"""Phase 15.12: external feedback schedules a fresh Cursor iteration and prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import (
    cursor_prompt_path,
    local_review_budget_used,
    read_cursor_prompt,
    record_local_review_for_budget,
)
from ai_dev_loop.legacy_pr_review_local_adapter import (
    begin_external_local_review_budget,
    derive_external_cursor_iteration_for_recovery,
    next_external_cursor_iteration,
    pending_external_cursor_iteration,
    scheduled_cursor_turn_from_legacy_pr_state,
)
from ai_dev_loop.resume_planner import LocalInvocationContext, WorkflowActionKind, plan_next_action
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    GithubPrReviewState,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    utc_now,
)
from ai_dev_loop.workflow_engine import _LocalLoopExecution

HEAD_SHA = "c29e15e6608a1111222233334444555566667777"
EXTERNAL_PROMPT = "Please fix the three cycle-2 threads exactly.\n"


def _sample_state(
    *,
    status: RunStatus = RunStatus.RUNNING_CURSOR,
    with_iteration_01: bool = True,
) -> RunState:
    now = utc_now()
    state = RunState(
        run_id="fixture-project-20260718T115934Z-176634",
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
            current_review_iteration=1 if with_iteration_01 else 0,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
    )
    if with_iteration_01:
        state.iterations = [
            {
                "number": 1,
                "kind": "initial_implementation",
                "cursor": {"prompt_path": "prompts/cursor-initial.txt"},
                "git": {"staged_diff_path": "git/diffs/01.patch"},
            }
        ]
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="fixing_external_feedback",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch="main",
        bound_head_sha=HEAD_SHA,
        request_comment_id="9002",
        request_marker="ai_dev_loop-pr-review:marker",
        request_created_at="2026-07-18T12:00:00+00:00",
        eligible_thread_ids=["PRRT_A", "PRRT_B", "PRRT_C"],
        expected_eligible_thread_ids=["PRRT_A", "PRRT_B", "PRRT_C"],
        processed_thread_ids=["PRRT_OLD_1", "PRRT_OLD_2"],
        resolved_thread_ids=["PRRT_OLD_1", "PRRT_OLD_2"],
        last_external_result_path="github/cycles/02/result.json",
        external_fix_prompt_path="prompts/fixes/github-02.txt",
        external_cursor_iteration=2 if with_iteration_01 else 1,
    )
    return state


def _legacy_context(state: RunState, run_directory: Path) -> LocalInvocationContext:
    turn = scheduled_cursor_turn_from_legacy_pr_state(state, run_directory)
    return LocalInvocationContext(scheduled_first_cursor_turn=turn)


def test_next_external_iteration_is_monotonic_over_completed_01() -> None:
    state = _sample_state(with_iteration_01=True)
    assert next_external_cursor_iteration(state) == 2
    assert pending_external_cursor_iteration(state) == 2
    assert derive_external_cursor_iteration_for_recovery(state) == 2


def test_plan_next_action_forces_cursor_on_fresh_iteration(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    iteration_dir = run_directory / "cursor" / "iterations" / "01"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "metadata.json").write_text(
        '{"exit_code": 0, "timed_out": false}',
        encoding="utf-8",
    )
    (run_directory / "prompts/fixes").mkdir(parents=True)
    (run_directory / "prompts/fixes/github-02.txt").write_text(EXTERNAL_PROMPT, encoding="utf-8")
    state = _sample_state(status=RunStatus.RUNNING_CURSOR, with_iteration_01=True)
    action = plan_next_action(state, run_directory, context=_legacy_context(state, run_directory))
    assert action is not None
    assert action.kind == WorkflowActionKind.CURSOR
    assert action.iteration_number == 2


def test_interrupted_plan_forces_cursor_before_staging(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    iteration_dir = run_directory / "cursor" / "iterations" / "01"
    iteration_dir.mkdir(parents=True)
    (iteration_dir / "metadata.json").write_text(
        '{"exit_code": 0, "timed_out": false}',
        encoding="utf-8",
    )
    (run_directory / "git" / "diffs").mkdir(parents=True)
    (run_directory / "git" / "diffs" / "01.patch").write_text("diff\n", encoding="utf-8")
    (run_directory / "prompts/fixes").mkdir(parents=True)
    (run_directory / "prompts/fixes/github-02.txt").write_text(EXTERNAL_PROMPT, encoding="utf-8")
    state = _sample_state(status=RunStatus.INTERRUPTED, with_iteration_01=True)
    # Historical buggy reset must still force Cursor when typed field is set.
    state.workflow.current_review_iteration = 0
    action = plan_next_action(state, run_directory, context=_legacy_context(state, run_directory))
    assert action is not None
    assert action.kind == WorkflowActionKind.CURSOR
    assert action.iteration_number == 2


def test_external_prompt_is_exact_github_02_bytes(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    prompt = run_directory / "prompts" / "fixes" / "github-02.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(EXTERNAL_PROMPT, encoding="utf-8")
    (run_directory / "prompts" / "cursor-initial.txt").parent.mkdir(parents=True, exist_ok=True)
    (run_directory / "prompts" / "cursor-initial.txt").write_text(
        "initial prompt must not be used\n", encoding="utf-8"
    )
    (run_directory / "prompts" / "fixes" / "01.txt").write_text(
        "local fix must not be used\n", encoding="utf-8"
    )
    state = _sample_state(with_iteration_01=True)
    scheduled = scheduled_cursor_turn_from_legacy_pr_state(state, run_directory)
    assert scheduled is not None
    assert (
        cursor_prompt_path(state, 2, scheduled_prompt_path=scheduled.prompt_path)
        == "prompts/fixes/github-02.txt"
    )
    assert (
        read_cursor_prompt(state, run_directory, 2, scheduled_prompt_path=scheduled.prompt_path)
        == EXTERNAL_PROMPT
    )


def test_empty_external_prompt_fails_closed(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    prompt = run_directory / "prompts" / "fixes" / "github-02.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("   \n", encoding="utf-8")
    state = _sample_state(with_iteration_01=True)
    with pytest.raises(ValidationError, match="cursor prompt is empty"):
        read_cursor_prompt(
            state,
            run_directory,
            2,
            scheduled_prompt_path="prompts/fixes/github-02.txt",
        )


def test_wrong_lifecycle_does_not_use_external_prompt(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    (run_directory / "prompts/fixes").mkdir(parents=True)
    (run_directory / "prompts/fixes/github-02.txt").write_text(EXTERNAL_PROMPT, encoding="utf-8")
    state = _sample_state(with_iteration_01=True)
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"lifecycle": "awaiting_bot_review"}
    )
    assert scheduled_cursor_turn_from_legacy_pr_state(state, run_directory) is None
    assert cursor_prompt_path(state, 2) == "prompts/fixes/01.txt"


def test_historical_without_typed_field_derives_for_recovery_only() -> None:
    state = _sample_state(with_iteration_01=True)
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"external_cursor_iteration": None}
    )
    assert pending_external_cursor_iteration(state) is None
    assert derive_external_cursor_iteration_for_recovery(state) == 2


def test_external_local_budget_decoupled_from_high_artifact_iteration() -> None:
    """A high artifact number must not exhaust max_review_iterations immediately."""

    state = _sample_state(with_iteration_01=True)
    state.workflow.max_review_iterations = 3
    state.workflow.current_review_iteration = 1
    # Simulate many prior durable iterations so the fresh external turn is high.
    state.iterations = [
        {"number": n, "kind": "cursor_correction" if n > 1 else "initial_implementation"}
        for n in range(1, 6)
    ]
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"external_cursor_iteration": 6}
    )
    begin_external_local_review_budget(state)
    assert state.workflow.local_review_count == 0
    assert local_review_budget_used(state) == 0

    # Post-external local Codex review (artifact 06) with actionable findings.
    state.workflow.current_review_iteration = 6
    budget_after = record_local_review_for_budget(state, iteration_number=6)
    assert budget_after == 1
    assert budget_after < state.workflow.max_review_iterations

    state.status = RunStatus.WAITING_FOR_CURSOR_FIX
    action = plan_next_action(state, Path("/tmp/run"))
    assert action is not None
    assert action.kind == WorkflowActionKind.CURSOR
    assert action.iteration_number == 7


def test_legacy_budget_still_uses_artifact_iteration_without_local_review_count() -> None:
    state = _sample_state(status=RunStatus.WAITING_FOR_CURSOR_FIX, with_iteration_01=True)
    state.workflow.max_review_iterations = 3
    state.workflow.current_review_iteration = 3
    state.workflow.local_review_count = None
    assert local_review_budget_used(state) == 3
    with pytest.raises(ValidationError, match="review iteration limit already reached"):
        plan_next_action(state, Path("/tmp/run"))


def test_apply_review_result_uses_local_budget_not_artifact_number(tmp_path: Path) -> None:
    from ai_dev_loop.review_result import CodexReviewResult
    from ai_dev_loop.workflow_engine import _apply_review_result

    run_directory = tmp_path / "run"
    run_directory.mkdir()
    state = _sample_state(status=RunStatus.REVIEWING, with_iteration_01=True)
    state.workflow.max_review_iterations = 3
    state.workflow.current_review_iteration = 6
    begin_external_local_review_budget(state)
    review = CodexReviewResult.model_validate(
        {
            "has_actionable_findings": True,
            "findings_count": 1,
            "highest_severity": "P2",
            "cursor_fix_prompt": "fix the issue",
            "review_markdown": "report",
            "tests_status": "not_applicable",
            "summary": "needs fix",
        }
    )
    execution = _LocalLoopExecution(invocation=LocalInvocationContext())
    _path, _msg, should_continue = _apply_review_result(
        run_directory,
        state,
        iteration_number=6,
        review=review,
        review_artifact_path="codex/reviews/06.json",
        loop_ctx=execution,
    )
    assert should_continue is True
    assert state.status == RunStatus.RUNNING_CURSOR
    assert state.workflow.local_review_count == 1
    assert state.status != RunStatus.MAX_ITERATIONS_REACHED

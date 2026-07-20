"""Phase 15.16: clean baseline for external Cursor; local corrections keep staged patch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.legacy_pr_review_local_adapter import (
    begin_external_local_review_budget,
    is_external_cursor_prompt_iteration,
    scheduled_cursor_turn_from_legacy_pr_state,
)
from ai_dev_loop.resume_planner import LocalInvocationContext, WorkflowActionKind, plan_next_action
from ai_dev_loop.runners.git import (
    validate_correction_pre_cursor,
    validate_external_feedback_pre_cursor,
)
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
    atomic_write_text,
    sha256_file,
    utc_now,
)
from ai_dev_loop.workflow_engine import _LocalLoopExecution, _run_cursor_turn


def _legacy_execution(state: RunState, run_directory: Path) -> _LocalLoopExecution:
    turn = scheduled_cursor_turn_from_legacy_pr_state(state, run_directory)
    return _LocalLoopExecution(invocation=LocalInvocationContext(scheduled_first_cursor_turn=turn))


EXTERNAL_PROMPT = "Please fix the two cycle-3 threads exactly.\n"
LOCAL_FIX_PROMPT = "Local Codex finding for iteration 03.\n"


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    plan = repo / "docs/plans/sample-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# plan\n", encoding="utf-8")
    prompt = repo / "docs/plans/prompt_sample-plan.txt"
    prompt.write_text("prompt\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _sample_external_state(
    repo: Path,
    *,
    external_iteration: int = 3,
    status: RunStatus = RunStatus.RUNNING_CURSOR,
) -> RunState:
    head = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    now = utc_now()
    state = RunState(
        run_id="fixture-project-20260719T002147Z-a0f030",
        project=ProjectRef(name="fixture-project"),
        status=status,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(repo),
            git_common_dir=str(repo / ".git"),
            git_dir=str(repo / ".git"),
            branch=branch,
            initial_head=head,
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256=sha256_file(repo / "docs/plans/sample-plan.md"),
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
            current_review_iteration=2,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
            local_review_count=0,
        ),
        iterations=[
            {
                "number": 1,
                "kind": "initial_implementation",
                "cursor": {"prompt_path": "prompts/cursor-initial.txt"},
                "git": {"staged_diff_path": "git/diffs/01.patch"},
            },
            {
                "number": 2,
                "kind": "cursor_correction",
                "cursor": {"prompt_path": "prompts/fixes/github-02.txt"},
                "git": {"staged_diff_path": "git/diffs/02.patch"},
            },
        ],
        github_pr_review=GithubPrReviewState(
            origin="source_run",
            source_run_id="source-local",
            lifecycle="fixing_external_feedback",
            cycle_number=3,
            max_external_cycles=8,
            pr_number=45,
            head_branch=branch,
            bound_head_sha=head,
            request_comment_id="9003",
            request_marker="ai_dev_loop-pr-review:marker",
            request_created_at="2026-07-19T00:21:00+00:00",
            eligible_thread_ids=["PRRT_C3A", "PRRT_C3B"],
            expected_eligible_thread_ids=["PRRT_C3A", "PRRT_C3B"],
            processed_thread_ids=["PRRT_OLD"],
            resolved_thread_ids=["PRRT_OLD"],
            last_external_result_path="github/cycles/03/result.json",
            external_fix_prompt_path="prompts/fixes/github-03.txt",
            external_cursor_iteration=external_iteration,
        ),
    )
    return state


def test_is_external_cursor_prompt_iteration_exclusive_to_typed_field(
    tiny_repo: Path,
) -> None:
    state = _sample_external_state(tiny_repo, external_iteration=3)
    assert is_external_cursor_prompt_iteration(state, 3) is True
    assert is_external_cursor_prompt_iteration(state, 2) is False
    assert is_external_cursor_prompt_iteration(state, 4) is False


def test_external_preflight_accepts_clean_repo_despite_historical_patch(
    tiny_repo: Path, tmp_path: Path
) -> None:
    """Clean published HEAD must not be compared to git/diffs/02.patch."""

    run_directory = tmp_path / "run"
    (run_directory / "git/diffs").mkdir(parents=True)
    # Historical published patch that cannot match a clean index.
    (run_directory / "git/diffs/02.patch").write_text(
        "diff --git a/old.txt b/old.txt\n+published\n",
        encoding="utf-8",
    )
    state = _sample_external_state(tiny_repo)
    validate_external_feedback_pre_cursor(state)
    with pytest.raises(ValidationError):
        validate_correction_pre_cursor(
            tiny_repo,
            patch_artifact=run_directory / "git/diffs/02.patch",
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ("staged", "empty staged index"),
        ("unstaged", "clean worktree"),
        ("untracked", "clean worktree"),
        ("head", "HEAD changed"),
        ("branch", "does not match PR head_branch"),
        ("bound", "initial_head does not match"),
    ],
)
def test_external_preflight_rejects_drift(tiny_repo: Path, mutate: str, match: str) -> None:
    state = _sample_external_state(tiny_repo)
    if mutate == "staged":
        target = tiny_repo / "staged.txt"
        target.write_text("staged\n", encoding="utf-8")
        _git(tiny_repo, "add", "staged.txt")
    elif mutate == "unstaged":
        (tiny_repo / "docs/plans/sample-plan.md").write_text("# dirty\n", encoding="utf-8")
    elif mutate == "untracked":
        (tiny_repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    elif mutate == "head":
        (tiny_repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tiny_repo, "add", "extra.txt")
        _git(tiny_repo, "commit", "-m", "advance")
    elif mutate == "branch":
        _git(tiny_repo, "checkout", "-b", "other-branch")
        state.repository.branch = "other-branch"
    elif mutate == "bound":
        assert state.github_pr_review is not None
        state.github_pr_review = state.github_pr_review.model_copy(
            update={"bound_head_sha": "a" * 40}
        )
    with pytest.raises(ValidationError, match=match):
        validate_external_feedback_pre_cursor(state)


def test_run_cursor_turn_uses_clean_baseline_and_skips_patch_compare(
    tiny_repo: Path, tmp_path: Path
) -> None:
    run_directory = tmp_path / "run"
    (run_directory / "git/diffs").mkdir(parents=True)
    (run_directory / "git/status").mkdir(parents=True)
    (run_directory / "git/cursor-output").mkdir(parents=True)
    (run_directory / "logs").mkdir(parents=True)
    (run_directory / "prompts/fixes").mkdir(parents=True)
    (run_directory / "git/diffs/02.patch").write_text(
        "diff --git a/old.txt b/old.txt\n+published\n",
        encoding="utf-8",
    )
    (run_directory / "prompts/fixes/github-03.txt").write_text(EXTERNAL_PROMPT, encoding="utf-8")
    state = _sample_external_state(tiny_repo)
    assert state.cursor.chat_id is not None

    fake_execution = MagicMock()
    fake_execution.process.returncode = 0
    fake_execution.process.elapsed_seconds = 0.1
    fake_execution.process.timed_out = False
    fake_execution.parse.parse_ok = True
    fake_execution.parse.errors = []
    fake_execution.parse.final_text = "done"
    fake_execution.metadata_args = ["agent", "-p", "redacted"]
    fake_execution.failure.is_usage_limit = False

    fingerprint = MagicMock()
    fingerprint.relative_path = "git/cursor-output/03.json"
    fingerprint.aggregate_sha256 = "c" * 64

    with (
        patch(
            "ai_dev_loop.workflow_engine.validate_correction_pre_cursor",
            side_effect=AssertionError("must not compare published patch"),
        ),
        patch(
            "ai_dev_loop.workflow_engine.execute_prompt",
            return_value=fake_execution,
        ) as execute,
        patch(
            "ai_dev_loop.workflow_engine.capture_cursor_output_fingerprint",
            return_value=fingerprint,
        ),
        patch(
            "ai_dev_loop.workflow_engine.capture_git_status",
            return_value="",
        ),
        patch(
            "ai_dev_loop.workflow_engine.validate_repository_identity",
        ),
        patch(
            "ai_dev_loop.workflow_engine.is_abort_requested",
            return_value=False,
        ),
    ):
        message = _run_cursor_turn(
            run_directory,
            state,
            chat_id=state.cursor.chat_id,
            iteration_number=3,
            loop_ctx=_legacy_execution(state, run_directory),
        )

    assert message == ""
    execute.assert_called_once()
    assert (run_directory / "cursor/iterations/03").is_dir()
    assert (run_directory / "cursor/iterations/03/metadata.json").is_file()
    assert state.status == RunStatus.STAGING


def test_run_cursor_turn_fails_before_creating_iteration_dir(
    tiny_repo: Path, tmp_path: Path
) -> None:
    run_directory = tmp_path / "run"
    (run_directory / "logs").mkdir(parents=True)
    (run_directory / "prompts/fixes").mkdir(parents=True)
    (run_directory / "prompts/fixes/github-03.txt").write_text(EXTERNAL_PROMPT, encoding="utf-8")
    (tiny_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    state = _sample_external_state(tiny_repo)
    assert state.cursor.chat_id is not None

    with pytest.raises(AiDevLoopError, match="clean worktree"):
        _run_cursor_turn(
            run_directory,
            state,
            chat_id=state.cursor.chat_id,
            iteration_number=3,
            loop_ctx=_legacy_execution(state, run_directory),
        )

    assert not (run_directory / "cursor/iterations/03").exists()
    assert state.status == RunStatus.FAILED


def test_local_correction_after_external_requires_03_patch(tiny_repo: Path, tmp_path: Path) -> None:
    """A local Codex finding on external iteration 03 schedules 04 against 03.patch."""

    run_directory = tmp_path / "run"
    (run_directory / "git/diffs").mkdir(parents=True)
    (run_directory / "prompts/fixes").mkdir(parents=True)
    (run_directory / "prompts/fixes/03.txt").write_text(LOCAL_FIX_PROMPT, encoding="utf-8")

    target = tiny_repo / "feature.txt"
    target.write_text("feature\n", encoding="utf-8")
    _git(tiny_repo, "add", "feature.txt")
    patch_text = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=tiny_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    atomic_write_text(run_directory / "git/diffs/03.patch", patch_text)

    state = _sample_external_state(tiny_repo, status=RunStatus.WAITING_FOR_CURSOR_FIX)
    assert state.github_pr_review is not None
    # External Cursor 03 completed; local review produced a fix prompt for 04.
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"lifecycle": "fixing_external_feedback", "external_cursor_iteration": 3}
    )
    begin_external_local_review_budget(state)
    state.workflow.local_review_count = 1
    state.workflow.current_review_iteration = 3
    state.iterations.append(
        {
            "number": 3,
            "kind": "cursor_correction",
            "cursor": {"prompt_path": "prompts/fixes/github-03.txt"},
            "git": {"staged_diff_path": "git/diffs/03.patch"},
            "codex": {"review_path": "codex/reviews/03.json"},
            "review": {"fix_prompt_path": "prompts/fixes/03.txt"},
        }
    )

    action = plan_next_action(state, run_directory)
    assert action is not None
    assert action.kind == WorkflowActionKind.CURSOR
    assert action.iteration_number == 4
    assert is_external_cursor_prompt_iteration(state, 4) is False

    validate_correction_pre_cursor(
        tiny_repo,
        patch_artifact=run_directory / "git/diffs/03.patch",
    )
    # Empty or drifted index still fails the local correction gate.
    _git(tiny_repo, "reset", "HEAD")
    with pytest.raises(ValidationError):
        validate_correction_pre_cursor(
            tiny_repo,
            patch_artifact=run_directory / "git/diffs/03.patch",
        )

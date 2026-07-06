"""Unit tests for Codex review runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.runners.codex import (
    build_codex_review_args,
    build_review_wrapper_prompt,
    redact_codex_args,
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


def _sample_state() -> RunState:
    now = utc_now()
    return RunState(
        run_id="fixture-project-20260704T134512Z-abc123",
        project=ProjectRef(name="fixture-project"),
        status=RunStatus.PREPARED,
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
            chat_id=None,
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=0,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
    )


def test_build_codex_review_args_option_order(tmp_path: Path) -> None:
    schema = tmp_path / "codex-review-result-v1.json"
    schema.write_text("{}", encoding="utf-8")
    result = tmp_path / "review.json"
    codex = _sample_state().codex
    args = build_codex_review_args(
        codex,
        repo_root="/tmp/repo",
        session_id=codex.session_id,
        schema_file=schema,
        result_file=result,
    )
    assert args[:4] == ["codex", "exec", "--cd", "/tmp/repo"]
    assert args[4:6] == ["--sandbox", "workspace-write"]
    assert args[6] == "resume"
    resume_index = args.index("resume")
    assert args[resume_index + 1 : resume_index + 3] == ["--model", "o4-mini"]
    assert "--json" in args
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert "--last" not in args
    assert args[-2] == codex.session_id
    assert args[-1] == "-"


def test_redact_codex_args_replaces_stdin_marker() -> None:
    args = ["codex", "exec", "resume", "session-id", "-"]
    assert redact_codex_args(args)[-1] == "<stdin-prompt>"


def test_review_wrapper_prompt_includes_skill_and_cursor_content(tmp_path: Path) -> None:
    state = _sample_state()
    run_directory = tmp_path / "run"
    (run_directory / "plan").mkdir(parents=True)
    (run_directory / "prompts").mkdir(parents=True)
    (run_directory / "cursor/iterations/01").mkdir(parents=True)
    (run_directory / "git/diffs").mkdir(parents=True)
    (run_directory / state.plan.snapshot_path).write_text("# plan\n", encoding="utf-8")
    (run_directory / state.prompt.snapshot_path).write_text(
        "Implement the sample plan exactly as written.\n",
        encoding="utf-8",
    )
    (run_directory / "cursor/iterations/01/final.txt").write_text(
        "done: Implement the sample plan",
        encoding="utf-8",
    )
    for rel in ("01.stat", "01.name-only.txt", "01.patch"):
        (run_directory / "git/diffs" / rel).write_text("artifact\n", encoding="utf-8")

    prompt = build_review_wrapper_prompt(
        state,
        run_directory,
        iteration="01",
        cursor_final_response="done: Implement the sample plan",
    )
    assert "$review-staged-cursor-execution" in prompt
    assert "Implement the sample plan exactly as written." in prompt
    assert "done: Implement the sample plan" in prompt
    assert "docs/plans/sample-plan.md" in prompt


def test_review_wrapper_prompt_states_missing_final_response(tmp_path: Path) -> None:
    state = _sample_state()
    run_directory = tmp_path / "run"
    (run_directory / "plan").mkdir(parents=True)
    (run_directory / "prompts").mkdir(parents=True)
    (run_directory / "cursor/iterations/01").mkdir(parents=True)
    (run_directory / "git/diffs").mkdir(parents=True)
    (run_directory / state.plan.snapshot_path).write_text("# plan\n", encoding="utf-8")
    (run_directory / state.prompt.snapshot_path).write_text("prompt\n", encoding="utf-8")

    prompt = build_review_wrapper_prompt(
        state,
        run_directory,
        iteration="01",
        cursor_final_response=None,
    )
    assert "could not extract a final Cursor response" in prompt


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "has_actionable_findings": True,
                "findings_count": 0,
                "highest_severity": "P1",
                "review_markdown": "# Review",
                "cursor_fix_prompt": "fix it",
                "tests_status": "passed",
                "summary": "bad",
            },
            "findings_count",
        ),
        (
            {
                "has_actionable_findings": False,
                "findings_count": 1,
                "highest_severity": None,
                "review_markdown": "# Review",
                "cursor_fix_prompt": None,
                "tests_status": "passed",
                "summary": "bad",
            },
            "findings_count",
        ),
        (
            {
                "has_actionable_findings": True,
                "findings_count": 1,
                "highest_severity": None,
                "review_markdown": "# Review",
                "cursor_fix_prompt": "fix it",
                "tests_status": "passed",
                "summary": "bad",
            },
            "highest_severity",
        ),
        (
            {
                "has_actionable_findings": False,
                "findings_count": 0,
                "highest_severity": "P1",
                "review_markdown": "# Review",
                "cursor_fix_prompt": None,
                "tests_status": "passed",
                "summary": "bad",
            },
            "highest_severity",
        ),
        (
            {
                "has_actionable_findings": True,
                "findings_count": 1,
                "highest_severity": "P1",
                "review_markdown": "# Review",
                "cursor_fix_prompt": None,
                "tests_status": "passed",
                "summary": "bad",
            },
            "cursor_fix_prompt",
        ),
        (
            {
                "has_actionable_findings": False,
                "findings_count": 0,
                "highest_severity": None,
                "review_markdown": "# Review",
                "cursor_fix_prompt": "fix it",
                "tests_status": "passed",
                "summary": "bad",
            },
            "cursor_fix_prompt",
        ),
    ],
)
def test_codex_review_result_cross_field_validation(payload: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CodexReviewResult.model_validate(payload)


def test_codex_review_result_accepts_valid_no_findings() -> None:
    result = CodexReviewResult.model_validate(
        {
            "has_actionable_findings": False,
            "findings_count": 0,
            "highest_severity": None,
            "review_markdown": "# Review\n\nOK",
            "cursor_fix_prompt": None,
            "tests_status": "passed",
            "summary": "No actionable findings.",
        }
    )
    assert result.findings_count == 0
    assert result.cursor_fix_prompt is None


def test_codex_review_result_accepts_valid_findings() -> None:
    result = CodexReviewResult.model_validate(
        json.loads(
            json.dumps(
                {
                    "has_actionable_findings": True,
                    "findings_count": 2,
                    "highest_severity": "P0",
                    "review_markdown": "# Review\n\nIssues",
                    "cursor_fix_prompt": "Fix both issues.",
                    "tests_status": "skipped_findings_present",
                    "summary": "Two findings.",
                }
            )
        )
    )
    assert result.has_actionable_findings is True
    assert result.cursor_fix_prompt == "Fix both issues."


def test_codex_failure_message_points_to_artifacts_without_raw_output() -> None:
    from ai_dev_loop.runners.codex import _codex_failure_message

    message = _codex_failure_message(
        exit_code=2,
        events_path="codex/events/01.jsonl",
        stderr_path="codex/events/01.stderr.txt",
    )
    assert message == (
        "Codex review failed with exit code 2; "
        "inspect codex/events/01.jsonl and codex/events/01.stderr.txt"
    )
    assert "proprietary" not in message

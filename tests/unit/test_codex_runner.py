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


def _build_args(
    tmp_path: Path,
    *,
    review_model: str | None = "o4-mini",
    review_reasoning_effort: str | None = None,
) -> list[str]:
    schema = tmp_path / "codex-review-result-v1.json"
    schema.write_text("{}", encoding="utf-8")
    result = tmp_path / "review.json"
    state = _sample_state()
    codex = state.codex.model_copy(
        update={
            "review_model": review_model,
            "review_reasoning_effort": review_reasoning_effort,
        }
    )
    return build_codex_review_args(
        codex,
        repo_root="/tmp/repo",
        session_id=codex.session_id,
        schema_file=schema,
        result_file=result,
    )


def test_build_codex_review_args_option_order(tmp_path: Path) -> None:
    args = _build_args(tmp_path)
    assert args[:4] == ["codex", "exec", "--cd", "/tmp/repo"]
    assert args[4:6] == ["--sandbox", "workspace-write"]
    assert args[6] == "resume"
    resume_index = args.index("resume")
    assert args[resume_index + 1 : resume_index + 3] == ["--model", "o4-mini"]
    assert "--json" in args
    assert "--output-schema" in args
    assert "--output-last-message" in args
    assert "--last" not in args
    assert args[-2] == "019abc00-0000-0000-0000-000000000000"
    assert args[-1] == "-"


def test_build_codex_review_args_inherits_model_and_reasoning(tmp_path: Path) -> None:
    args = _build_args(tmp_path, review_model=None, review_reasoning_effort=None)
    resume_index = args.index("resume")
    assert args[resume_index + 1] == "--json"
    assert "--model" not in args
    assert "-c" not in args
    assert "model_reasoning_effort" not in " ".join(args)
    assert "--last" not in args
    assert args[-2] == "019abc00-0000-0000-0000-000000000000"


def test_build_codex_review_args_model_only(tmp_path: Path) -> None:
    args = _build_args(tmp_path, review_model="gpt-5.5", review_reasoning_effort=None)
    resume_index = args.index("resume")
    assert args[resume_index + 1 : resume_index + 4] == ["--model", "gpt-5.5", "--json"]
    assert "-c" not in args


def test_build_codex_review_args_reasoning_only(tmp_path: Path) -> None:
    args = _build_args(tmp_path, review_model=None, review_reasoning_effort="high")
    resume_index = args.index("resume")
    assert args[resume_index + 1 : resume_index + 4] == [
        "-c",
        'model_reasoning_effort="high"',
        "--json",
    ]
    assert "--model" not in args


def test_build_codex_review_args_model_and_reasoning(tmp_path: Path) -> None:
    args = _build_args(tmp_path, review_model="gpt-5.5", review_reasoning_effort="high")
    resume_index = args.index("resume")
    assert args[resume_index + 1 : resume_index + 6] == [
        "--model",
        "gpt-5.5",
        "-c",
        'model_reasoning_effort="high"',
        "--json",
    ]
    assert "--last" not in args


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


def test_ensure_codex_review_artifact_dirs_is_idempotent(tmp_path: Path) -> None:
    from ai_dev_loop.runners.codex import ensure_codex_review_artifact_dirs

    events = tmp_path / "codex" / "events" / "01.jsonl"
    result = tmp_path / "codex" / "reviews" / "01.json"
    preexisting = tmp_path / "codex" / "reviews" / "partial.json"
    preexisting.parent.mkdir(parents=True)
    preexisting.write_text('{"keep": true}\n', encoding="utf-8")

    ensure_codex_review_artifact_dirs(events, result, preexisting)
    ensure_codex_review_artifact_dirs(events, result, preexisting)

    assert events.parent.is_dir()
    assert result.parent.is_dir()
    assert preexisting.read_text(encoding="utf-8") == '{"keep": true}\n'


def test_run_codex_review_creates_output_parent_before_fake_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake Codex fails if --output-last-message parent is missing before launch."""

    import os
    import stat
    from unittest.mock import patch

    from ai_dev_loop.paths import schema_path
    from ai_dev_loop.runners.codex import run_codex_review

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
assert "--output-last-message" in args
output = args[args.index("--output-last-message") + 1]
parent = os.path.dirname(output)
if not parent or not os.path.isdir(parent):
    print(f"missing parent: {parent!r}", file=sys.stderr)
    sys.exit(91)
sys.stdin.read()
with open(output, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "has_actionable_findings": False,
            "findings_count": 0,
            "highest_severity": None,
            "review_markdown": "# Review\\n\\nOK",
            "cursor_fix_prompt": None,
            "tests_status": "passed",
            "summary": "No actionable findings.",
        },
        handle,
    )
print(json.dumps({"type": "message", "content": "ok"}))
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    run_directory = tmp_path / "run"
    (run_directory / "plan").mkdir(parents=True)
    (run_directory / "prompts").mkdir(parents=True)
    (run_directory / "cursor/iterations/01").mkdir(parents=True)
    (run_directory / "git/diffs").mkdir(parents=True)
    state = _sample_state()
    state = state.model_copy(
        update={
            "codex": state.codex.model_copy(update={"command": "codex"}),
            "repository": state.repository.model_copy(update={"root": str(tmp_path / "repo")}),
            "iterations": [
                {
                    "number": 1,
                    "kind": "initial_implementation",
                    "started_at": state.created_at.isoformat(),
                    "cursor": {"chat_id": "chat-1"},
                    "git": {"staged_diff_path": "git/diffs/01.patch"},
                }
            ],
        }
    )
    (tmp_path / "repo").mkdir()
    (run_directory / state.plan.snapshot_path).write_text("# plan\n", encoding="utf-8")
    (run_directory / state.prompt.snapshot_path).write_text("prompt\n", encoding="utf-8")
    (run_directory / "cursor/iterations/01/final.txt").write_text("done\n", encoding="utf-8")
    for rel in ("01.stat", "01.name-only.txt", "01.patch"):
        (run_directory / "git/diffs" / rel).write_text("artifact\n", encoding="utf-8")

    assert not (run_directory / "codex" / "reviews").exists()
    with (
        patch(
            "ai_dev_loop.runners.codex.schema_path",
            return_value=schema_path("codex-review-result-v1.json"),
        ),
        patch("ai_dev_loop.runners.codex.validate_codex_response_schema"),
    ):
        execution = run_codex_review(state, run_directory, iteration="01")

    assert execution.result.has_actionable_findings is False
    assert (run_directory / "codex/reviews/01.json").is_file()
    assert (run_directory / "codex/reviews/01.md").is_file()
    assert (run_directory / "codex/reviews/01.metadata.json").is_file()
    assert (run_directory / "codex/events/01.jsonl").is_file()


def test_run_codex_review_preserves_events_on_fake_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import stat

    import pytest as pytest_mod

    from ai_dev_loop.errors import AiDevLoopError
    from ai_dev_loop.runners.codex import run_codex_review

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
output = args[args.index("--output-last-message") + 1]
parent = os.path.dirname(output)
if not parent or not os.path.isdir(parent):
    print(f"missing parent: {parent!r}", file=sys.stderr)
    sys.exit(91)
sys.stdin.read()
print("events", flush=True)
print("codex failed", file=sys.stderr)
sys.exit(2)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    run_directory = tmp_path / "run"
    (run_directory / "plan").mkdir(parents=True)
    (run_directory / "prompts").mkdir(parents=True)
    (run_directory / "cursor/iterations/01").mkdir(parents=True)
    (run_directory / "git/diffs").mkdir(parents=True)
    state = _sample_state()
    state = state.model_copy(
        update={
            "repository": state.repository.model_copy(update={"root": str(tmp_path / "repo")}),
            "iterations": [
                {
                    "number": 1,
                    "kind": "initial_implementation",
                    "started_at": state.created_at.isoformat(),
                    "cursor": {},
                    "git": {"staged_diff_path": "git/diffs/01.patch"},
                }
            ],
        }
    )
    (tmp_path / "repo").mkdir()
    (run_directory / state.plan.snapshot_path).write_text("# plan\n", encoding="utf-8")
    (run_directory / state.prompt.snapshot_path).write_text("prompt\n", encoding="utf-8")
    for rel in ("01.stat", "01.name-only.txt", "01.patch"):
        (run_directory / "git/diffs" / rel).write_text("artifact\n", encoding="utf-8")

    from unittest.mock import patch

    from ai_dev_loop.paths import schema_path

    with (
        patch(
            "ai_dev_loop.runners.codex.schema_path",
            return_value=schema_path("codex-review-result-v1.json"),
        ),
        patch("ai_dev_loop.runners.codex.validate_codex_response_schema"),
        pytest_mod.raises(AiDevLoopError, match="exit code 2"),
    ):
        run_codex_review(state, run_directory, iteration="01")

    assert (run_directory / "codex/events/01.jsonl").is_file()
    assert (run_directory / "codex/events/01.stderr.txt").is_file()
    assert (run_directory / "codex/reviews/01.metadata.json").is_file()
    assert not (run_directory / "codex/reviews/01.json").exists()


def test_classify_codex_output_artifact_failure_requires_durable_evidence(tmp_path: Path) -> None:
    from ai_dev_loop.runners.codex import (
        FAILURE_CODE_RESULT_ARTIFACT_MISSING,
        classify_codex_review_output_artifact_failure,
    )

    reviews = tmp_path / "codex" / "reviews"
    events = tmp_path / "codex" / "events"
    reviews.mkdir(parents=True)
    events.mkdir(parents=True)

    (reviews / "01.metadata.json").write_text(
        json.dumps({"exit_code": 2, "timed_out": False}),
        encoding="utf-8",
    )
    (events / "01.stderr.txt").write_text("codex review failed\n", encoding="utf-8")
    assert classify_codex_review_output_artifact_failure(tmp_path, "01") is False

    (reviews / "01.metadata.json").write_text(
        json.dumps({"exit_code": 2, "timed_out": True}),
        encoding="utf-8",
    )
    assert classify_codex_review_output_artifact_failure(tmp_path, "01") is False

    (reviews / "01.metadata.json").write_text(
        json.dumps(
            {
                "exit_code": 2,
                "timed_out": False,
                "failure_code": FAILURE_CODE_RESULT_ARTIFACT_MISSING,
            }
        ),
        encoding="utf-8",
    )
    assert classify_codex_review_output_artifact_failure(tmp_path, "01") is True

    (reviews / "01.metadata.json").write_text(
        json.dumps({"exit_code": 2, "timed_out": False}),
        encoding="utf-8",
    )
    (events / "01.stderr.txt").write_text(
        "failed to write output-last-message codex/reviews/01.json: "
        "No such file or directory (os error 2)\n",
        encoding="utf-8",
    )
    assert classify_codex_review_output_artifact_failure(tmp_path, "01") is True

    # Historical Codex CLI releases could report this write failure to stderr
    # but still exit zero. The bound missing-file evidence remains sufficient;
    # do not depend on the user-facing state.last_error string.
    (reviews / "01.metadata.json").write_text(
        json.dumps({"exit_code": 0, "timed_out": False}),
        encoding="utf-8",
    )
    assert classify_codex_review_output_artifact_failure(tmp_path, "01") is True

"""Integration tests for Codex review after Git staging."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.runners.codex import PHASE_4_NO_FINDINGS_MESSAGE
from ai_dev_loop.state import RunStatus, load_run_state

runner = CliRunner()


def test_start_completes_after_no_finding_codex_review(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    result = start_run(prepared_run["run_id"])
    assert result.status == "completed"
    assert PHASE_4_NO_FINDINGS_MESSAGE in result.result_message

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.COMPLETED
    assert state.workflow.current_review_iteration == 1
    assert state.result == PHASE_4_NO_FINDINGS_MESSAGE

    iteration = state.iterations[0]
    assert "codex" in iteration
    assert "review" in iteration
    assert iteration["review"]["has_actionable_findings"] is False
    assert iteration["codex"]["result_path"] == "codex/reviews/01.json"

    for rel in (
        "codex/events/01.jsonl",
        "codex/events/01.stderr.txt",
        "codex/reviews/01.json",
        "codex/reviews/01.md",
        "codex/reviews/01.metadata.json",
        "logs/events.jsonl",
    ):
        assert (run_path / rel).is_file()

    codex_log = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert "019abc00-0000-0000-0000-000000000000" in codex_log
    assert "--last" not in codex_log
    assert "$review-staged-cursor-execution" in codex_log
    assert "Implement the sample plan exactly as written." in codex_log


def test_start_waiting_for_cursor_fix_after_findings(prepared_run, fake_clis, monkeypatch) -> None:
    """Single-review findings without sequence stop at waiting_for_cursor_fix when max=1."""
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "")
    from io import StringIO
    from unittest.mock import patch

    from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
    from ai_dev_loop.paths import run_dir

    prompt = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"
    prompt_text = (prompt / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    repo = prepared_run["repo"]
    with patch("sys.stdin", StringIO(prompt_text)):
        prepared = prepare_run(
            PrepareOptions(
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
                max_review_iterations=1,
            )
        )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    result = start_run(prepared.run_id)
    assert result.status == "max_iterations_reached"

    run_path = run_dir("fixture-project", prepared.run_id)
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.MAX_ITERATIONS_REACHED
    assert state.iterations[0]["review"]["has_actionable_findings"] is True
    assert state.iterations[0]["codex"]["fix_prompt_path"] == "prompts/fixes/01.txt"
    fix_prompt = (run_path / "prompts/fixes/01.txt").read_text(encoding="utf-8")
    assert "Fix the sample issue" in fix_prompt


def test_start_completed_with_residual_risk_for_blocked_environment(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "blocked_environment")
    result = start_run(prepared_run["run_id"])
    assert result.status == "completed_with_residual_risk"

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.COMPLETED_WITH_RESIDUAL_RISK
    assert state.iterations[0]["review"]["tests_status"] == "blocked_environment"


def test_start_fails_on_invalid_codex_review_json(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "invalid_json")
    with pytest.raises(AiDevLoopError, match="valid JSON"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert (prepared_run["run_path"] / "codex/events/01.jsonl").is_file()


def test_start_fails_on_codex_nonzero_exit(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    with pytest.raises(AiDevLoopError, match="Codex review failed with exit code 2"):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    expected_error = (
        "Codex review failed with exit code 2; "
        "inspect codex/events/01.jsonl and codex/events/01.stderr.txt"
    )
    assert state.last_error == expected_error

    human_log = (run_path / "logs" / "ai_dev_loop.log").read_text(encoding="utf-8")
    assert expected_error in human_log


def test_start_marks_interrupted_on_codex_timeout(prepared_run, fake_clis, monkeypatch) -> None:
    from ai_dev_loop.process import StreamingProcessResult

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    def timed_out_streaming(args, **kwargs):
        return StreamingProcessResult(
            args=list(args),
            returncode=124,
            stdout='{"type":"message"}',
            stderr="",
            timed_out=True,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr("ai_dev_loop.runners.codex.run_process_streaming", timed_out_streaming)

    with pytest.raises(AiDevLoopError, match="timed out"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.INTERRUPTED


def test_codex_command_uses_cd_and_sandbox_before_resume(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])
    codex_log = fake_clis["codex_log"].read_text(encoding="utf-8")
    args_line = next(line for line in codex_log.splitlines() if line.startswith("ARGS:"))
    args = eval(args_line.removeprefix("ARGS:"))  # noqa: S307
    cd_index = args.index("--cd")
    sandbox_index = args.index("--sandbox")
    resume_index = args.index("resume")
    assert cd_index < resume_index
    assert sandbox_index < resume_index


def test_events_jsonl_does_not_contain_full_prompt(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])
    events = (prepared_run["run_path"] / "logs/events.jsonl").read_text(encoding="utf-8")
    assert "Implement the sample plan exactly as written." not in events
    assert "token=" not in events.lower() or "<redacted>" in events


def test_logs_component_codex_shows_review_summary(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])
    result = runner.invoke(app, ["logs", prepared_run["run_id"], "--component", "codex"])
    assert result.exit_code == 0
    assert "review 1 summary" in result.stdout
    assert "has_actionable_findings" in result.stdout
    assert "content redacted" in result.stdout
    assert "Fix the sample issue" not in result.stdout


def test_logs_component_codex_redacts_findings_prompt(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    start_run(prepared_run["run_id"])
    result = runner.invoke(app, ["logs", prepared_run["run_id"], "--component", "codex"])
    assert result.exit_code == 0
    assert "review 1 summary" in result.stdout
    assert '"cursor_fix_prompt": "<redacted>"' in result.stdout
    assert "Fix the sample issue" not in result.stdout
    assert "Found issue." not in result.stdout
    assert "019abc00-0000-0000-0000-000000000000" not in result.stdout


def test_status_reports_completed_next_action(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])
    result = runner.invoke(app, ["status", prepared_run["run_id"]])
    assert result.exit_code == 0
    assert "no actionable findings" in result.stdout.lower()


def test_cli_start_output_reports_phase4_result(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "Status: completed" in combined
    assert "no actionable findings" in combined.lower()
    assert "correction execution is not implemented" not in combined.lower()


def test_staged_changes_remain_after_completed_review(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=prepared_run["repo"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout.strip()


def test_prepare_creates_events_jsonl(git_repo, isolated_xdg, fake_clis) -> None:
    from io import StringIO
    from unittest.mock import patch

    from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run

    prompt = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"
    prompt_text = (prompt / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt_text)):
        prepared = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )
    from ai_dev_loop.paths import run_dir

    events_path = run_dir("fixture-project", prepared.run_id) / "logs/events.jsonl"
    assert events_path.is_file()
    payload = json.loads(events_path.read_text(encoding="utf-8").strip())
    assert payload["event"] == "prepare_completed"

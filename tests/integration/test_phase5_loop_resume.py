"""Phase 5 integration tests for bounded loop and resume."""

from __future__ import annotations

import re
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.extend import extend_review_iterations
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.commands.resume import resume_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import run_dir
from ai_dev_loop.state import RunStatus, load_run_state, save_run_state

runner = CliRunner()
FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _prepare(repo: Path, *, max_review_iterations: int = 3) -> str:
    prompt_text = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt_text)):
        prepared = prepare_run(
            PrepareOptions(
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
                max_review_iterations=max_review_iterations,
            )
        )
    return prepared.run_id


def test_start_findings_then_no_findings_completes_two_iterations(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    result = start_run(prepared_run["run_id"])
    assert result.status == "completed"
    assert result.iteration_count == 2

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert len(state.iterations) == 2
    assert state.iterations[0]["kind"] == "initial_implementation"
    assert state.iterations[1]["kind"] == "cursor_correction"
    assert (run_path / "prompts/fixes/01.txt").is_file()
    assert (run_path / "cursor/iterations/02/events.jsonl").is_file()
    assert (run_path / "codex/reviews/02.json").is_file()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    resume_ids = re.findall(r"'--resume', '([^']+)'", agent_log)
    assert len(resume_ids) >= 2
    assert len(set(resume_ids)) == 1

    codex_log = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert codex_log.count("019abc00-0000-0000-0000-000000000000") >= 2
    assert "--last" not in codex_log

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=prepared_run["repo"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout.strip()


def test_start_max_iterations_reached_without_extra_cursor_turn(
    git_repo, isolated_xdg, fake_clis, monkeypatch, fixture_codex_session
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
    run_id = _prepare(git_repo, max_review_iterations=3)
    result = start_run(run_id)
    assert result.status == "max_iterations_reached"

    run_path = run_dir("fixture-project", run_id)
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.MAX_ITERATIONS_REACHED
    assert state.workflow.current_review_iteration == 3
    assert (run_path / "prompts/fixes/03.txt").is_file()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_log.count("-p") == 3


def test_extend_maxed_run_resumes_stored_final_fix_prompt(
    git_repo, isolated_xdg, fake_clis, monkeypatch, fixture_codex_session
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
    run_id = _prepare(git_repo, max_review_iterations=3)

    first_result = start_run(run_id)
    assert first_result.status == "max_iterations_reached"

    extension = extend_review_iterations(run_id, additional_review_iterations=1)
    assert extension.status == "waiting_for_cursor_fix"
    assert extension.current_review_iteration == 3
    assert extension.previous_max_review_iterations == 3
    assert extension.max_review_iterations == 4

    run_path = run_dir("fixture-project", run_id)
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.WAITING_FOR_CURSOR_FIX
    assert state.workflow.current_review_iteration == 3
    assert state.workflow.max_review_iterations == 4
    assert (run_path / "prompts/fixes/03.txt").is_file()
    assert "review_iterations_extended" in (run_path / "logs/events.jsonl").read_text(
        encoding="utf-8"
    )

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    resumed = resume_run(run_id)
    assert resumed.status == "completed"
    assert resumed.iteration_count == 4

    state = load_run_state(run_path / "state.json")
    assert state.workflow.current_review_iteration == 4
    assert (run_path / "cursor/iterations/04/events.jsonl").is_file()
    assert (run_path / "codex/reviews/04.json").is_file()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_log.count("-p") == 4


def test_resume_from_waiting_for_cursor_fix(prepared_run, fake_clis, monkeypatch) -> None:
    from ai_dev_loop import workflow_engine

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "")

    original_apply = workflow_engine._apply_review_result

    def checkpoint_apply(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        _, _, should_continue = result
        if should_continue:
            run_directory, state = args[0], args[1]
            state.status = RunStatus.WAITING_FOR_CURSOR_FIX
            save_run_state(run_directory, state)
            raise AiDevLoopError("checkpointed for resume test")
        return result

    monkeypatch.setattr(workflow_engine, "_apply_review_result", checkpoint_apply)

    with pytest.raises(AiDevLoopError, match="checkpointed"):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.WAITING_FOR_CURSOR_FIX

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "correction")
    result = resume_run(prepared_run["run_id"])
    assert result.status == "completed"

    state = load_run_state(run_path / "state.json")
    assert len(state.iterations) == 2
    assert (run_path / "git/diffs/02.patch").is_file()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    resume_ids = re.findall(r"'--resume', '([^']+)'", agent_log)
    assert len(set(resume_ids)) == 1


def test_resume_from_staging_skips_cursor(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.STAGING
    state.workflow.current_review_iteration = 0
    save_run_state(run_path, state)

    agent_log_before = fake_clis["agent_log"].read_text(encoding="utf-8")
    calls_before = agent_log_before.count("ARGS:")

    result = resume_run(prepared_run["run_id"])
    assert result.status == "completed"

    agent_log_after = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_log_after.count("ARGS:") == calls_before


def test_resume_from_reviewing_processes_existing_review(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.REVIEWING
    state.result = None
    save_run_state(run_path, state)

    codex_log_before = fake_clis["codex_log"].read_text(encoding="utf-8")
    calls_before = codex_log_before.count("ARGS:")

    result = resume_run(prepared_run["run_id"])
    assert result.status == "completed"
    assert codex_log_before.count("ARGS:") == calls_before


def test_resume_preserves_partial_codex_artifacts_on_retry(
    prepared_run, fake_clis, monkeypatch
) -> None:
    from ai_dev_loop.resume_planner import preserve_artifact_before_retry

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    events_path = run_path / "codex/events/01.jsonl"
    original_events = events_path.read_text(encoding="utf-8")
    events_path.unlink()

    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.INTERRUPTED
    save_run_state(run_path, state)

    temp_events = run_path / "codex/events/01.retry.jsonl"
    temp_events.write_text('{"type":"partial"}\n', encoding="utf-8")
    preserve_artifact_before_retry(temp_events)

    preserved = list((run_path / "codex/events").glob("01.retry.attempt-*.jsonl"))
    assert preserved
    assert temp_events.read_text(encoding="utf-8") == '{"type":"partial"}\n'
    assert original_events


def test_resume_refuses_terminal_status(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    with pytest.raises(AiDevLoopError, match="terminal status"):
        resume_run(prepared_run["run_id"])


def test_status_and_inspect_report_multiple_iterations(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    start_run(prepared_run["run_id"])

    status = runner.invoke(app, ["status", prepared_run["run_id"]])
    assert status.exit_code == 0
    assert "Recorded iterations: 2" in status.stdout

    inspect = runner.invoke(app, ["inspect", prepared_run["run_id"]])
    assert inspect.exit_code == 0
    assert "number: 1" in inspect.stdout
    assert "number: 2" in inspect.stdout

    logs = runner.invoke(app, ["logs", prepared_run["run_id"], "--component", "codex"])
    assert logs.exit_code == 0
    assert "review 1 summary" in logs.stdout
    assert "review 2 summary" in logs.stdout

    events = (prepared_run["run_path"] / "logs/events.jsonl").read_text(encoding="utf-8")
    assert "Fix the sample issue" not in events


def test_resume_interrupted_after_cursor_completes_staging(
    prepared_run, fake_clis, monkeypatch
) -> None:
    from ai_dev_loop import workflow_engine
    from ai_dev_loop.commands.start_preflight import mark_interrupted

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = prepared_run["run_path"]

    def interrupt_before_staging(state, *args, **kwargs):
        mark_interrupted(state, "interrupted after cursor for test")
        save_run_state(run_path, state)
        raise AiDevLoopError("interrupted after cursor for test")

    monkeypatch.setattr(workflow_engine, "begin_staging", interrupt_before_staging)

    with pytest.raises(AiDevLoopError, match="interrupted after cursor"):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.INTERRUPTED

    result = resume_run(prepared_run["run_id"])
    assert result.status == "completed"


def test_resume_interrupted_after_staging_runs_review_only(
    prepared_run, fake_clis, monkeypatch
) -> None:
    from ai_dev_loop import workflow_engine
    from ai_dev_loop.commands.start_preflight import mark_interrupted

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    original_staging = workflow_engine._run_staging_pass

    def staging_then_interrupt(run_directory, state, *, iteration_number):
        original_staging(run_directory, state, iteration_number=iteration_number)
        mark_interrupted(state, "interrupted after staging for test")
        save_run_state(run_directory, state)
        raise AiDevLoopError("interrupted after staging for test")

    monkeypatch.setattr(workflow_engine, "_run_staging_pass", staging_then_interrupt)

    with pytest.raises(AiDevLoopError, match="interrupted after staging"):
        start_run(prepared_run["run_id"])

    codex_log_path = fake_clis["codex_log"]
    codex_log_before = (
        codex_log_path.read_text(encoding="utf-8") if codex_log_path.is_file() else ""
    )
    calls_before = codex_log_before.count("ARGS:")

    result = resume_run(prepared_run["run_id"])
    assert result.status == "completed"
    assert fake_clis["codex_log"].read_text(encoding="utf-8").count("ARGS:") == calls_before + 1


def test_resume_interrupted_with_existing_review_processes_outcome(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.INTERRUPTED
    state.result = None
    save_run_state(run_path, state)

    codex_log_before = fake_clis["codex_log"].read_text(encoding="utf-8")
    calls_before = codex_log_before.count("ARGS:")

    result = resume_run(prepared_run["run_id"])
    assert result.status == "completed"
    assert fake_clis["codex_log"].read_text(encoding="utf-8").count("ARGS:") == calls_before


def test_resume_rejects_missing_chat_id_on_checkpoint(prepared_run, fake_clis, monkeypatch) -> None:

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.REVIEWING
    state.cursor.chat_id = None
    save_run_state(run_path, state)

    with pytest.raises(AiDevLoopError, match="cannot create a new chat"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error == (
        "cursor chat id is missing from checkpointed run state; cannot create a new chat"
    )


def test_resume_rejects_staged_index_drift_before_review_processing(
    prepared_run, fake_clis, monkeypatch
) -> None:

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]
    subprocess.run(["git", "reset", "HEAD", "ai_dev_loop.yaml"], cwd=repo, check=True)

    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.REVIEWING
    state.result = None
    save_run_state(run_path, state)

    with pytest.raises(AiDevLoopError, match="no longer matches"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "no longer matches" in state.last_error


def test_resume_correction_preflight_failure_marks_failed(
    prepared_run, fake_clis, monkeypatch
) -> None:
    _checkpoint_run_at_waiting_for_cursor_fix(prepared_run, fake_clis, monkeypatch)

    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]
    subprocess.run(["git", "reset", "HEAD", "ai_dev_loop.yaml"], cwd=repo, check=True)

    with pytest.raises(AiDevLoopError, match="no longer matches"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "no longer matches" in state.last_error


def _checkpoint_run_at_waiting_for_cursor_fix(
    prepared_run,
    fake_clis,
    monkeypatch,
) -> None:
    from ai_dev_loop import workflow_engine

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "")

    original_apply = workflow_engine._apply_review_result

    def checkpoint_apply(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        _, _, should_continue = result
        if should_continue:
            run_directory, state = args[0], args[1]
            state.status = RunStatus.WAITING_FOR_CURSOR_FIX
            save_run_state(run_directory, state)
            raise AiDevLoopError("checkpointed for resume test")
        return result

    monkeypatch.setattr(workflow_engine, "_apply_review_result", checkpoint_apply)

    with pytest.raises(AiDevLoopError, match="checkpointed"):
        start_run(prepared_run["run_id"])


def test_resume_missing_fix_prompt_marks_failed(prepared_run, fake_clis, monkeypatch) -> None:
    _checkpoint_run_at_waiting_for_cursor_fix(prepared_run, fake_clis, monkeypatch)

    run_path = prepared_run["run_path"]
    fix_prompt = run_path / "prompts/fixes/01.txt"
    fix_prompt.unlink()

    with pytest.raises(AiDevLoopError, match="cursor prompt missing"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "cursor prompt missing" in state.last_error


def test_resume_empty_fix_prompt_marks_failed(prepared_run, fake_clis, monkeypatch) -> None:
    _checkpoint_run_at_waiting_for_cursor_fix(prepared_run, fake_clis, monkeypatch)

    run_path = prepared_run["run_path"]
    fix_prompt = run_path / "prompts/fixes/01.txt"
    fix_prompt.write_text("   \n", encoding="utf-8")

    with pytest.raises(AiDevLoopError, match="cursor prompt is empty"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "cursor prompt is empty" in state.last_error


def test_resume_rejects_unstaged_changes_before_review_processing(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]
    target = repo / "ai_dev_loop.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "\n# unstaged drift\n", encoding="utf-8")

    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.REVIEWING
    state.result = None
    save_run_state(run_path, state)

    with pytest.raises(AiDevLoopError, match="unstaged tracked changes"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "unstaged tracked changes" in state.last_error


def test_resume_rejects_untracked_files_before_review_processing(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]
    (repo / "unrelated_drift.txt").write_text("extra work\n", encoding="utf-8")

    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.REVIEWING
    state.result = None
    save_run_state(run_path, state)

    with pytest.raises(AiDevLoopError, match="untracked files"):
        resume_run(prepared_run["run_id"])

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error is not None
    assert "untracked files" in state.last_error


def test_resume_waiting_at_max_iterations_marks_max_iterations_reached(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.WAITING_FOR_CURSOR_FIX
    state.workflow.current_review_iteration = state.workflow.max_review_iterations
    save_run_state(run_path, state)

    agent_log_before = fake_clis["agent_log"].read_text(encoding="utf-8")
    calls_before = agent_log_before.count("ARGS:")

    result = resume_run(prepared_run["run_id"])
    assert result.status == "max_iterations_reached"
    assert fake_clis["agent_log"].read_text(encoding="utf-8").count("ARGS:") == calls_before

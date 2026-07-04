"""Integration tests for Git staging after Cursor execution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.runners.staging import PHASE_3_BOUNDARY_MESSAGE
from ai_dev_loop.state import RunStatus, load_run_state

runner = CliRunner()


def test_start_stages_tracked_modifications(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]

    result = start_run(prepared_run["run_id"])
    assert result.status == "staging"
    assert result.staged_diff_path == "git/diffs/01.patch"

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.STAGING
    assert state.result == PHASE_3_BOUNDARY_MESSAGE
    assert len(state.iterations) == 1
    iteration = state.iterations[0]
    assert iteration["kind"] == "initial_implementation"
    assert iteration["git"]["staged_diff_path"] == "git/diffs/01.patch"
    assert "codex" not in iteration

    for rel in (
        "git/status/01-before-staging.txt",
        "git/status/01-after-staging.txt",
        "git/diffs/01.stat",
        "git/diffs/01.name-only.txt",
        "git/diffs/01.patch",
    ):
        assert (run_path / rel).is_file()

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout.strip()


def test_start_stages_untracked_files(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "untracked")
    result = start_run(prepared_run["run_id"])
    assert result.status == "staging"
    name_only = (prepared_run["run_path"] / "git/diffs/01.name-only.txt").read_text(
        encoding="utf-8"
    )
    assert "new_feature.txt" in name_only


def test_start_does_not_stage_after_cursor_failure(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 1
    run_path = prepared_run["run_path"]
    assert not (run_path / "git/diffs/01.patch").exists()
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED


def test_start_does_not_stage_after_cursor_timeout(prepared_run, fake_clis, monkeypatch) -> None:
    from ai_dev_loop.runners import cursor as cursor_runner

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "2")

    original_execute = cursor_runner.execute_prompt

    def short_timeout_execute(*args, **kwargs):
        kwargs["timeout_seconds"] = 0.5
        return original_execute(*args, **kwargs)

    monkeypatch.setattr("ai_dev_loop.commands.start.execute_prompt", short_timeout_execute)

    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 1
    assert not (prepared_run["run_path"] / "git/diffs/01.patch").exists()
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.INTERRUPTED


def test_start_rejects_cursor_self_staging(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "stage_self")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert "pre-existing staged" in (state.last_error or "").lower()


def test_start_rejects_tracked_prompt_changes(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "modify_prompt")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert "prompt source" in (state.last_error or "").lower()
    assert not (prepared_run["run_path"] / "git/diffs/01.patch").exists()


def test_start_rejects_plan_changes_after_cursor(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "modify_plan")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert "plan" in (state.last_error or "").lower()


def test_start_fails_when_git_add_fails(prepared_run, fake_clis, monkeypatch) -> None:
    def failing_add(repo_root: Path):
        from ai_dev_loop.process import ProcessResult

        return ProcessResult(
            args=["git", "add", "-A"],
            returncode=1,
            stdout="",
            stderr="simulated git add failure",
        )

    monkeypatch.setattr("ai_dev_loop.runners.staging.git_add_all", failing_add)

    with pytest.raises(AiDevLoopError, match="git add -A failed"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED


def test_start_marks_failed_when_staging_git_command_fails(
    prepared_run, fake_clis, monkeypatch
) -> None:
    def failing_status(repo_root: Path) -> str:
        raise AiDevLoopError("git status: git status failed: simulated porcelain failure")

    monkeypatch.setattr("ai_dev_loop.runners.staging.git_status_porcelain", failing_status)

    with pytest.raises(AiDevLoopError, match="simulated porcelain failure"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error and "simulated porcelain failure" in state.last_error


def test_start_marks_failed_when_staging_artifact_write_fails(
    prepared_run, fake_clis, monkeypatch
) -> None:
    def failing_write(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr("ai_dev_loop.runners.staging.atomic_write_text", failing_write)

    with pytest.raises(AiDevLoopError, match="Git staging artifact capture failed"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error and "Git staging artifact capture failed" in state.last_error


def test_start_fails_when_no_staged_changes(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "none")

    with pytest.raises(AiDevLoopError, match="no staged changes"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED


def test_status_reports_phase3_boundary(prepared_run, fake_clis) -> None:
    start_run(prepared_run["run_id"])
    result = runner.invoke(app, ["status", prepared_run["run_id"]])
    assert result.exit_code == 0
    assert "Git staging is complete" in result.stdout
    assert "Codex review" in result.stdout
    assert "not implemented yet" in result.stdout


def test_cli_start_output_reports_phase3_boundary(prepared_run, fake_clis) -> None:
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "Staged diff: git/diffs/01.patch" in combined
    assert "Git staging is complete" in combined
    assert "Codex review" in combined


def test_staged_patch_uses_sensitive_permissions(
    git_repo: Path,
    fake_clis,
    permission_test_root,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import stat
    from io import StringIO

    from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run

    state_home = permission_test_root / "xdg-state"
    config_home = permission_test_root / "xdg-config"
    cache_home = permission_test_root / "xdg-cache"
    for path in (state_home, config_home, cache_home):
        path.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))

    repo = git_repo
    fixture_prompt = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"
    prompt = (fixture_prompt / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        prepared = prepare_run(
            PrepareOptions(
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )

    start_run(prepared.run_id)
    from ai_dev_loop.paths import run_dir

    patch_path = run_dir("fixture-project", prepared.run_id) / "git/diffs/01.patch"
    assert patch_path.is_file()
    assert stat.S_IMODE(patch_path.stat().st_mode) == SENSITIVE_FILE_MODE

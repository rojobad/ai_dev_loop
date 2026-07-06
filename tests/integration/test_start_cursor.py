"""Integration tests for start, Cursor execution, and locks."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, LockError
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.paths import SENSITIVE_FILE_MODE, run_dir
from ai_dev_loop.state import RunStatus, load_run_state, save_run_state

FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"

runner = CliRunner()


def test_start_rejects_unknown_run_id(isolated_xdg) -> None:
    result = runner.invoke(app, ["start", "missing-run"])
    assert result.exit_code == 4
    assert "run not found" in (result.stderr or result.stdout).lower()


def test_start_rejects_non_prepared_status(prepared_run, fake_clis) -> None:
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.STAGING
    save_run_state(run_path, state)
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4


def test_start_happy_path_writes_artifacts(prepared_run, fake_clis) -> None:
    run_id = prepared_run["run_id"]
    run_path = prepared_run["run_path"]

    result = start_run(run_id)
    assert result.status == "completed"
    assert result.chat_id == "019abc00-1111-2222-3333-444444444444"

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.COMPLETED
    assert state.cursor.chat_id == result.chat_id
    assert (run_path / "cursor" / "chat.json").is_file()
    iteration = run_path / "cursor" / "iterations" / "01"
    assert (iteration / "events.jsonl").is_file()
    assert (iteration / "stderr.txt").is_file()
    assert (iteration / "metadata.json").is_file()
    assert (iteration / "final.txt").is_file()
    assert (run_path / "git" / "status" / "01-before-cursor.txt").is_file()
    assert (run_path / "git" / "status" / "01-after-cursor.txt").is_file()
    assert (run_path / "git" / "diffs" / "01.patch").is_file()
    assert (run_path / "codex" / "reviews" / "01.json").is_file()
    assert state.result and "no actionable findings" in state.result.lower()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert "Implement the sample plan exactly as written." in agent_log


def test_start_reuses_existing_chat_id(prepared_run, fake_clis) -> None:
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.cursor.chat_id = "019abc00-aaaa-bbbb-cccc-dddddddddddd"
    save_run_state(run_path, state)

    result = start_run(prepared_run["run_id"])
    assert result.chat_id == "019abc00-aaaa-bbbb-cccc-dddddddddddd"
    assert "create-chat" not in fake_clis["agent_log"].read_text(encoding="utf-8")


def test_start_rejects_branch_drift(prepared_run, fake_clis) -> None:
    repo = prepared_run["repo"]
    subprocess.run(["git", "checkout", "-b", "other-branch"], cwd=repo, check=True)
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED


def test_start_rejects_cursor_auth_failure(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_AUTH", "false")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4
    assert "probe failed" in (result.stderr or result.stdout).lower()


def test_start_rejects_missing_model(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODELS", "other-model")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4


def test_start_records_cursor_failure(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 1
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.cursor.chat_id
    assert (run_path / "cursor" / "iterations" / "01" / "events.jsonl").exists()


def test_start_marks_failed_when_execute_prompt_raises(
    prepared_run, fake_clis, monkeypatch
) -> None:
    def raise_launch_error(*args, **kwargs):
        raise AiDevLoopError("Cursor launch failed: agent not found")

    monkeypatch.setattr("ai_dev_loop.commands.start.execute_prompt", raise_launch_error)

    with pytest.raises(AiDevLoopError, match="Cursor launch failed"):
        start_run(prepared_run["run_id"])

    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.last_error == "Cursor launch failed: agent not found"


def test_start_timeout_records_interrupted(prepared_run, fake_clis, monkeypatch) -> None:
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
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.INTERRUPTED


def test_status_remains_readable_while_locked(prepared_run, fake_clis) -> None:
    run_path = prepared_run["run_path"]
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=prepared_run["run_id"],
        repository_path=str(prepared_run["repo"]),
        started_at=load_run_state(run_path / "state.json").created_at,
    )
    locks = RunLocks(run_path, metadata)
    locks.acquire()
    try:
        result = runner.invoke(app, ["status", prepared_run["run_id"]])
        assert result.exit_code == 0
    finally:
        locks.release()


def test_run_lock_blocks_second_start(prepared_run, fake_clis) -> None:
    run_path = prepared_run["run_path"]
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=prepared_run["run_id"],
        repository_path=str(prepared_run["repo"]),
        started_at=load_run_state(run_path / "state.json").created_at,
    )
    locks = RunLocks(run_path, metadata)
    locks.acquire()
    try:
        with pytest.raises(LockError):
            start_run(prepared_run["run_id"])
    finally:
        locks.release()


def test_start_does_not_mutate_run_when_lock_unavailable(prepared_run, fake_clis) -> None:
    run_path = prepared_run["run_path"]
    log_path = run_path / "logs" / "ai_dev_loop.log"
    state_path = run_path / "state.json"
    log_before = log_path.read_text(encoding="utf-8")
    state_before = state_path.read_text(encoding="utf-8")
    log_mtime_before = log_path.stat().st_mtime

    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=prepared_run["run_id"],
        repository_path=str(prepared_run["repo"]),
        started_at=load_run_state(state_path).created_at,
    )
    locks = RunLocks(run_path, metadata)
    locks.acquire()
    try:
        with pytest.raises(LockError):
            start_run(prepared_run["run_id"])
        assert log_path.read_text(encoding="utf-8") == log_before
        assert state_path.read_text(encoding="utf-8") == state_before
        assert log_path.stat().st_mtime == log_mtime_before
    finally:
        locks.release()


def test_cli_start_output_reports_phase4_result(prepared_run, fake_clis) -> None:
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "Status: completed" in combined
    assert "no actionable findings" in combined.lower()
    assert "Codex review" in combined


def test_cli_start_prints_codex_warning_before_success_output(prepared_run, fake_clis) -> None:
    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 0
    warning_index = result.stdout.index("Codex TUI")
    started_index = result.stdout.index("Started run")
    assert warning_index < started_index


def test_start_missing_cursor_executable_marks_failed(prepared_run, fake_clis, monkeypatch) -> None:
    git_executable = shutil.which("git")
    assert git_executable is not None
    git_bin = str(Path(git_executable).parent)
    (fake_clis["bin_dir"] / "agent").unlink()
    monkeypatch.setenv("PATH", f"{fake_clis['bin_dir']}{os.pathsep}{git_bin}")

    result = runner.invoke(app, ["start", prepared_run["run_id"]])
    assert result.exit_code == 4
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED
    assert "executable not found" in (result.stderr or result.stdout).lower()


def test_cursor_artifacts_use_sensitive_permissions(
    git_repo: Path,
    fake_clis,
    permission_test_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = permission_test_root / "xdg-state"
    config_home = permission_test_root / "xdg-config"
    cache_home = permission_test_root / "xdg-cache"
    for path in (state_home, config_home, cache_home):
        path.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        prepared = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )

    start_run(prepared.run_id)
    iteration = run_dir("fixture-project", prepared.run_id) / "cursor" / "iterations" / "01"
    for name in ("events.jsonl", "stderr.txt", "metadata.json", "final.txt"):
        artifact = iteration / name
        assert artifact.is_file()
        assert stat.S_IMODE(artifact.stat().st_mode) == SENSITIVE_FILE_MODE

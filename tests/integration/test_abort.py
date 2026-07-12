"""Integration tests for real abort behavior."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.abort_control import (
    ABORT_REQUEST_REL_PATH,
    ACTIVE_PROCESS_REL_PATH,
)
from ai_dev_loop.cli import app
from ai_dev_loop.commands.abort import run_abort
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.state import RunStatus, load_run_state
from ai_dev_loop.workflow_engine import ABORT_RESULT_MESSAGE

runner = CliRunner()
FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "sample_repo"


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_active_component(run_path: Path, component: str, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = run_path / ACTIVE_PROCESS_REL_PATH
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("component") == component:
                return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for active {component} process")


def test_abort_prepared_run_without_active_workflow(prepared_run) -> None:
    result = run_abort(prepared_run["run_id"])
    assert result.status == "aborted"
    assert result.abort_requested is True

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.ABORTED
    assert (run_path / ABORT_REQUEST_REL_PATH).is_file()


def test_abort_refuses_terminal_completed_run(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    start_run(prepared_run["run_id"])

    result = run_abort(prepared_run["run_id"])
    assert result.abort_requested is False
    assert "terminal status completed" in result.message
    assert load_run_state(prepared_run["run_path"] / "state.json").status == RunStatus.COMPLETED


def test_abort_running_cursor_process(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "30")

    outcome: dict[str, object] = {}

    def run_start() -> None:
        outcome["result"] = start_run(prepared_run["run_id"])

    thread = threading.Thread(target=run_start)
    thread.start()
    try:
        run_path = prepared_run["run_path"]
        _wait_for_file(run_path / ACTIVE_PROCESS_REL_PATH)
        abort_result = run_abort(prepared_run["run_id"])
        assert abort_result.active_process_signaled is True
        thread.join(timeout=15)
        assert not thread.is_alive()
        workflow_result = outcome["result"]
        assert workflow_result.status == "aborted"
        state = load_run_state(run_path / "state.json")
        assert state.status == RunStatus.ABORTED
        assert ABORT_RESULT_MESSAGE in (state.result or "")
    finally:
        thread.join(timeout=1)


def test_abort_running_codex_process(prepared_run, fake_clis, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "sleep")
    monkeypatch.setenv("FAKE_CODEX_SLEEP_SECONDS", "30")

    outcome: dict[str, object] = {}

    def run_start() -> None:
        outcome["result"] = start_run(prepared_run["run_id"])

    thread = threading.Thread(target=run_start)
    thread.start()
    try:
        run_path = prepared_run["run_path"]
        _wait_for_active_component(run_path, "codex")
        abort_result = run_abort(prepared_run["run_id"])
        assert abort_result.active_process_signaled is True
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert outcome["result"].status == "aborted"
    finally:
        thread.join(timeout=1)


def test_abort_preserves_repository_changes(prepared_run, fake_clis, monkeypatch) -> None:
    repo = prepared_run["repo"]
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "sleep")
    monkeypatch.setenv("FAKE_CODEX_SLEEP_SECONDS", "30")

    snapshot: dict[str, str] = {"staged": "", "worktree": ""}

    def run_start() -> None:
        start_run(prepared_run["run_id"])

    thread = threading.Thread(target=run_start)
    thread.start()
    try:
        run_path = prepared_run["run_path"]
        _wait_for_active_component(run_path, "codex")
        staged = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        snapshot["staged"] = staged.stdout
        snapshot["worktree"] = status.stdout
        assert snapshot["staged"].strip()
        run_abort(prepared_run["run_id"])
        thread.join(timeout=15)
    finally:
        thread.join(timeout=1)

    staged_after = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    status_after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged_after.stdout == snapshot["staged"]
    assert status_after.stdout == snapshot["worktree"]


def test_abort_does_not_continue_to_staging_after_request(
    prepared_run, fake_clis, monkeypatch
) -> None:
    from ai_dev_loop import workflow_engine

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    original_execute = workflow_engine.execute_prompt

    def slow_execute(*args, **kwargs):
        result = original_execute(*args, **kwargs)
        run_abort(prepared_run["run_id"])
        return result

    monkeypatch.setattr(workflow_engine, "execute_prompt", slow_execute)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")

    result = start_run(prepared_run["run_id"])
    assert result.status == "aborted"
    run_path = prepared_run["run_path"]
    assert not (run_path / "git/diffs/01.patch").is_file()
    assert load_run_state(run_path / "state.json").status == RunStatus.ABORTED


def test_abort_stale_metadata_is_not_signaled(tmp_path: Path, prepared_run) -> None:
    from ai_dev_loop.abort_control import register_active_process

    run_path = prepared_run["run_path"]
    register_active_process(
        run_path,
        run_id="wrong-run-id",
        component="cursor",
        iteration=1,
        pid=999999,
        pgid=999999,
        parent_pid=999998,
        cwd=str(tmp_path),
        argv_redacted=["agent"],
    )
    result = run_abort(prepared_run["run_id"])
    assert result.active_process_signaled is False
    assert result.signal_outcome == "stale"
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.ABORTED


def test_abort_stale_live_process_does_not_mark_aborted(prepared_run, tmp_path: Path) -> None:
    from ai_dev_loop.abort_control import register_active_process

    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    run_path = prepared_run["run_path"]
    try:
        register_active_process(
            run_path,
            run_id=prepared_run["run_id"],
            component="cursor",
            iteration=1,
            pid=proc.pid,
            pgid=pgid,
            parent_pid=999998,
            cwd=str(tmp_path),
            argv_redacted=["agent", "<prompt-redacted>"],
        )
        result = run_abort(prepared_run["run_id"])
        assert result.active_process_signaled is False
        assert result.signal_outcome == "stale"
        assert result.status == "prepared"
        assert "not marked aborted" in result.message.lower()
        state = load_run_state(run_path / "state.json")
        assert state.status == RunStatus.PREPARED
        assert (run_path / ABORT_REQUEST_REL_PATH).is_file()
        assert (run_path / ACTIVE_PROCESS_REL_PATH).is_file()
    finally:
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=2)


def test_abort_during_probes_skips_cursor_chat_creation(prepared_run, monkeypatch) -> None:
    from ai_dev_loop import workflow_engine

    original_probes = workflow_engine._run_probes

    def probes_then_abort(run_directory, state) -> None:
        run_abort(prepared_run["run_id"])
        original_probes(run_directory, state)

    monkeypatch.setattr(workflow_engine, "_run_probes", probes_then_abort)

    result = start_run(prepared_run["run_id"])
    assert result.status == "aborted"
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id is None
    assert not (run_path / "cursor" / "chat.json").is_file()


def test_child_sigterm_without_abort_request_is_not_treated_as_abort(
    prepared_run, monkeypatch
) -> None:
    from ai_dev_loop import workflow_engine
    from ai_dev_loop.process import StreamingProcessResult
    from ai_dev_loop.runners.cursor import CursorExecutionResult, CursorParseResult
    from ai_dev_loop.runners.cursor_failure import classify_cursor_failure_text

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "none")

    def killed_cursor(*args, **kwargs):
        process = StreamingProcessResult(
            args=["agent"],
            returncode=-signal.SIGTERM,
            stdout="",
            stderr="killed externally",
            timed_out=False,
            elapsed_seconds=0.1,
        )
        parse = CursorParseResult(final_text=None, errors=(), parse_ok=False)
        return CursorExecutionResult(
            process=process,
            parse=parse,
            metadata_args=["agent", "<prompt-redacted>"],
            failure=classify_cursor_failure_text(
                returncode=process.returncode,
                timed_out=process.timed_out,
                stderr=process.stderr,
                structured_errors=parse.errors,
            ),
        )

    monkeypatch.setattr(workflow_engine, "execute_prompt", killed_cursor)

    with pytest.raises(AiDevLoopError, match="Cursor execution failed"):
        start_run(prepared_run["run_id"])
    state = load_run_state(prepared_run["run_path"] / "state.json")
    assert state.status == RunStatus.FAILED


def test_status_and_inspect_show_abort_diagnostics(prepared_run) -> None:
    run_abort(prepared_run["run_id"])
    status = runner.invoke(app, ["status", prepared_run["run_id"]])
    assert status.exit_code == 0
    assert "Abort request: pending" in status.stdout
    assert "aborted" in status.stdout

    inspect = runner.invoke(app, ["inspect", prepared_run["run_id"]])
    assert inspect.exit_code == 0
    assert "abort_request_path" in inspect.stdout
    assert "active_process_path" in inspect.stdout


def test_cli_abort_command(prepared_run) -> None:
    result = runner.invoke(app, ["abort", prepared_run["run_id"]])
    assert result.exit_code == 0
    assert "Abort requested: yes" in result.stdout
    assert "aborted" in result.stdout


def test_resume_refuses_aborted_run(prepared_run) -> None:
    run_abort(prepared_run["run_id"])
    result = runner.invoke(app, ["resume", prepared_run["run_id"]])
    assert result.exit_code != 0
    combined = (result.stdout + result.stderr).lower()
    assert "terminal status" in combined or "aborted" in combined

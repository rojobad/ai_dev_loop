"""Regression tests for launcher readiness, locking, identity, and policy."""

from __future__ import annotations

import os
import threading
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import write_session_rollout

from ai_dev_loop.commands.launch import launch_run
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.launcher import (
    LauncherOutcome,
    LauncherRecord,
    detached_worker_environment,
    mark_launcher_finished,
    process_identity_matches,
    read_launcher_policy,
    read_launcher_record,
    read_process_starttime,
    spawn_detached_worker,
    validate_launcher_record,
    wait_for_launcher_ready,
    write_launcher_policy,
    write_launcher_record,
)
from ai_dev_loop.runners.tool_updates import ToolUpdateFlags, UpdateMode
from ai_dev_loop.state import utc_now

CONTROLLER_ID = "019abc00-aaaa-0000-0000-0000000000aa"
REVIEWER_ID = "019abc00-bbbb-0000-0000-0000000000bb"


@pytest.fixture
def ab_codex_home(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    codex_home = isolated_home / ".codex"
    write_session_rollout(
        codex_home / "sessions",
        session_id=REVIEWER_ID,
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


def _prepare_ab(git_repo: Path):
    prompt = "Implement the approved plan.\n"
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_ID,
                codex_review_model="gpt-5.6-sol",
                codex_review_reasoning_effort="high",
            )
        )


def test_detached_worker_environment_drops_drvfs_codex_state_only() -> None:
    environment = detached_worker_environment(
        {
            "CODEX_HOME": "/mnt/c/Users/WinUser/.codex",
            "CODEX_SQLITE_HOME": "/mnt/c/Users/WinUser/.codex/state.sqlite",
            "PATH": "/usr/local/bin:/usr/bin",
            "FAKE_CODEX_REVIEW_MODE": "no_findings",
        }
    )

    assert "CODEX_HOME" not in environment
    assert "CODEX_SQLITE_HOME" not in environment
    assert environment["PATH"] == "/usr/local/bin:/usr/bin"
    assert environment["FAKE_CODEX_REVIEW_MODE"] == "no_findings"


def test_detached_worker_environment_preserves_native_wsl_overrides() -> None:
    environment = detached_worker_environment(
        {
            "CODEX_HOME": "/home/reviewer/.codex",
            "CODEX_SQLITE_HOME": "/var/lib/reviewer-codex",
        }
    )

    assert environment["CODEX_HOME"] == "/home/reviewer/.codex"
    assert environment["CODEX_SQLITE_HOME"] == "/var/lib/reviewer-codex"


def test_immediate_exit_worker_does_not_leave_running_record(
    git_repo: Path, isolated_xdg, ab_codex_home, monkeypatch
) -> None:
    prepared = _prepare_ab(git_repo)
    real_popen = __import__("subprocess").Popen

    def immediate_exit_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        import subprocess
        import sys

        return real_popen(
            [sys.executable, "-c", "raise SystemExit(1)"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            cwd=kwargs.get("cwd"),
            shell=False,
            start_new_session=True,
            close_fds=True,
        )

    monkeypatch.setattr("ai_dev_loop.launcher.subprocess.Popen", immediate_exit_popen)

    with pytest.raises(ValidationError, match="exited before"):
        spawn_detached_worker(prepared.run_directory, run_id=prepared.run_id)

    record = read_launcher_record(prepared.run_directory)
    assert record is not None
    assert record.outcome == LauncherOutcome.FAILED
    assert record.ready_at is None
    validation = validate_launcher_record(prepared.run_directory, run_id=prepared.run_id)
    assert validation is not None
    assert validation.is_live is False


def test_fast_successful_worker_is_not_treated_as_preready_failure(
    git_repo: Path, isolated_xdg, ab_codex_home, monkeypatch
) -> None:
    prepared = _prepare_ab(git_repo)
    real_popen = __import__("subprocess").Popen
    real_write = write_launcher_record

    def immediate_exit_popen(*args, **kwargs):  # type: ignore[no-untyped-def]
        import subprocess
        import sys

        return real_popen(
            [sys.executable, "-c", "raise SystemExit(0)"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            cwd=kwargs.get("cwd"),
            shell=False,
            start_new_session=True,
            close_fds=True,
        )

    def write_with_fast_worker_success(run_directory: Path, record: LauncherRecord) -> None:
        real_write(run_directory, record)
        if record.outcome == LauncherOutcome.RUNNING and record.ready_at is None:
            # Worker consumed the record, acknowledged readiness, and finished.
            real_write(
                run_directory,
                record.model_copy(
                    update={
                        "ready_at": utc_now(),
                        "outcome": LauncherOutcome.COMPLETED,
                        "finished_at": utc_now(),
                        "exit_code": 0,
                        "workflow_status": "completed",
                        "safe_error": None,
                    }
                ),
            )

    monkeypatch.setattr("ai_dev_loop.launcher.subprocess.Popen", immediate_exit_popen)
    monkeypatch.setattr(
        "ai_dev_loop.launcher.write_launcher_record", write_with_fast_worker_success
    )

    result = spawn_detached_worker(prepared.run_directory, run_id=prepared.run_id)
    assert result.outcome == LauncherOutcome.COMPLETED
    assert result.ready_at is not None
    final = read_launcher_record(prepared.run_directory)
    assert final is not None
    assert final.outcome == LauncherOutcome.COMPLETED
    assert final.safe_error is None
    assert "before becoming ready" not in (final.safe_error or "")


def test_parallel_launch_spawns_only_one_worker(
    git_repo: Path, isolated_xdg, ab_codex_home, monkeypatch
) -> None:
    prepared = _prepare_ab(git_repo)
    spawn_calls: list[str] = []
    entered = threading.Event()
    release_spawn = threading.Event()
    call_lock = threading.Lock()

    def slow_spawn(run_directory, *, run_id, policy=None):  # type: ignore[no-untyped-def]
        with call_lock:
            spawn_calls.append(run_id)
        entered.set()
        assert release_spawn.wait(timeout=5)
        record = LauncherRecord(
            run_id=run_id,
            worker_token=f"token-{len(spawn_calls)}",
            pid=os.getpid(),
            pgid=os.getpgid(os.getpid()),
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=read_process_starttime(os.getpid()),
            ready_at=utc_now(),
            argv_redacted=["python", "-m", "ai_dev_loop.launch_worker", "<run-id>", "<token>"],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.RUNNING,
        )
        write_launcher_record(run_directory, record)
        return record

    monkeypatch.setattr("ai_dev_loop.commands.launch.spawn_detached_worker", slow_spawn)

    results: list[object] = []
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID))
        except BaseException as exc:  # noqa: BLE001 — collect for assertion
            errors.append(exc)

    first = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=invoke)
    second.start()
    time.sleep(0.2)
    release_spawn.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not errors
    assert len(results) == 2
    already = [result.already_running for result in results]  # type: ignore[attr-defined]
    assert already.count(False) == 1
    assert already.count(True) == 1
    assert len(spawn_calls) == 1


def test_concurrent_launch_reloads_state_after_first_worker_finishes(
    git_repo: Path, isolated_xdg, ab_codex_home, monkeypatch
) -> None:
    prepared = _prepare_ab(git_repo)
    spawn_calls: list[str] = []
    entered = threading.Event()
    release_spawn = threading.Event()

    def finishing_spawn(run_directory, *, run_id, policy=None):  # type: ignore[no-untyped-def]
        from ai_dev_loop.state import RunStatus, load_run_state, save_run_state

        spawn_calls.append(run_id)
        entered.set()
        assert release_spawn.wait(timeout=5)
        state = load_run_state(run_directory / "state.json")
        state.status = RunStatus.COMPLETED
        state.result = "completed by first worker"
        save_run_state(run_directory, state)
        record = LauncherRecord(
            run_id=run_id,
            worker_token="finished-token",
            pid=os.getpid(),
            pgid=os.getpgid(os.getpid()),
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=read_process_starttime(os.getpid()),
            ready_at=utc_now(),
            argv_redacted=["python", "-m", "ai_dev_loop.launch_worker", "<run-id>", "<token>"],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.COMPLETED,
            finished_at=utc_now(),
            exit_code=0,
            workflow_status="completed",
        )
        write_launcher_record(run_directory, record)
        return record

    monkeypatch.setattr("ai_dev_loop.commands.launch.spawn_detached_worker", finishing_spawn)

    first_result: list[object] = []
    second_errors: list[BaseException] = []

    def first_invoke() -> None:
        first_result.append(launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID))

    def second_invoke() -> None:
        try:
            launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID)
        except BaseException as exc:  # noqa: BLE001 — collect for assertion
            second_errors.append(exc)

    first = threading.Thread(target=first_invoke)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=second_invoke)
    second.start()
    time.sleep(0.2)
    release_spawn.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert len(first_result) == 1
    assert len(spawn_calls) == 1
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], ValidationError)
    assert "terminal run" in str(second_errors[0]) or "prepared" in str(second_errors[0])


def test_mark_launcher_finished_requires_matching_worker_token(
    git_repo: Path, isolated_xdg, ab_codex_home
) -> None:
    prepared = _prepare_ab(git_repo)
    write_launcher_record(
        prepared.run_directory,
        LauncherRecord(
            run_id=prepared.run_id,
            worker_token="owner-token",
            pid=12345,
            pgid=12345,
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=99,
            argv_redacted=["python", "-m", "ai_dev_loop.launch_worker", "<run-id>", "<token>"],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.RUNNING,
        ),
    )
    assert (
        mark_launcher_finished(
            prepared.run_directory,
            run_id=prepared.run_id,
            worker_token="other-token",
            pid=12345,
            exit_code=1,
            workflow_status="failed",
            safe_error="loser",
        )
        is False
    )
    record = read_launcher_record(prepared.run_directory)
    assert record is not None
    assert record.outcome == LauncherOutcome.RUNNING
    assert record.safe_error is None

    assert (
        mark_launcher_finished(
            prepared.run_directory,
            run_id=prepared.run_id,
            worker_token="owner-token",
            pid=12345,
            exit_code=0,
            workflow_status="completed",
            safe_error=None,
        )
        is True
    )
    record = read_launcher_record(prepared.run_directory)
    assert record is not None
    assert record.outcome == LauncherOutcome.COMPLETED


def test_reused_pid_with_mismatched_starttime_is_stale(
    git_repo: Path, isolated_xdg, ab_codex_home
) -> None:
    prepared = _prepare_ab(git_repo)
    current_start = read_process_starttime(os.getpid())
    assert current_start is not None
    write_launcher_record(
        prepared.run_directory,
        LauncherRecord(
            run_id=prepared.run_id,
            worker_token="token",
            pid=os.getpid(),
            pgid=os.getpgid(os.getpid()),
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=current_start + 999999,
            argv_redacted=["python", "-m", "ai_dev_loop.launch_worker", "<run-id>", "<token>"],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.RUNNING,
        ),
    )
    ok, reason = process_identity_matches(read_launcher_record(prepared.run_directory))  # type: ignore[arg-type]
    assert ok is False
    assert reason == "launcher starttime mismatch"
    validation = validate_launcher_record(prepared.run_directory, run_id=prepared.run_id)
    assert validation is not None
    assert validation.is_live is False
    assert validation.is_stale is True


def test_launch_passes_explicit_tool_policy(
    git_repo: Path, isolated_xdg, ab_codex_home, monkeypatch
) -> None:
    prepared = _prepare_ab(git_repo)
    captured: dict[str, object] = {}

    def capture_spawn(run_directory, *, run_id, policy=None):  # type: ignore[no-untyped-def]
        captured["policy"] = policy
        if policy is not None:
            write_launcher_policy(run_directory, policy)
        record = LauncherRecord(
            run_id=run_id,
            worker_token="token",
            pid=os.getpid(),
            pgid=os.getpgid(os.getpid()),
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=read_process_starttime(os.getpid()),
            argv_redacted=["python", "-m", "ai_dev_loop.launch_worker", "<run-id>", "<token>"],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.RUNNING,
        )
        write_launcher_record(run_directory, record)
        return record

    monkeypatch.setattr("ai_dev_loop.commands.launch.spawn_detached_worker", capture_spawn)

    launch_run(
        prepared.run_id,
        controller_session_id=CONTROLLER_ID,
        tool_flags=ToolUpdateFlags(update_tools=True, allow_incompatible_tools=True),
    )
    policy = captured["policy"]
    assert policy is not None
    assert policy.update_mode == "always"  # type: ignore[attr-defined]
    assert policy.allow_incompatible is True  # type: ignore[attr-defined]
    loaded = read_launcher_policy(prepared.run_directory / "locks" / "launcher-policy.json")
    assert loaded.update_mode == "always"
    assert loaded.allow_incompatible is True


def test_launch_rejects_contradictory_tool_flags(
    git_repo: Path, isolated_xdg, ab_codex_home
) -> None:
    prepared = _prepare_ab(git_repo)
    with pytest.raises(Exception, match="contradictory"):
        launch_run(
            prepared.run_id,
            controller_session_id=CONTROLLER_ID,
            tool_flags=ToolUpdateFlags(update_tools=True, skip_tool_update=True),
        )


def test_wait_for_launcher_ready_requires_matching_pid_and_token(
    git_repo: Path, isolated_xdg, ab_codex_home
) -> None:
    prepared = _prepare_ab(git_repo)
    token = "ready-token"

    def publisher() -> None:
        time.sleep(0.1)
        write_launcher_record(
            prepared.run_directory,
            LauncherRecord(
                run_id=prepared.run_id,
                worker_token=token,
                pid=os.getpid(),
                pgid=os.getpgid(os.getpid()),
                parent_pid=1,
                started_at=utc_now(),
                pid_starttime=read_process_starttime(os.getpid()),
                argv_redacted=["python", "-m", "ai_dev_loop.launch_worker", "<run-id>", "<token>"],
                stdout_path="logs/launcher.stdout.txt",
                stderr_path="logs/launcher.stderr.txt",
                outcome=LauncherOutcome.RUNNING,
            ),
        )

    # Wrong pid should time out quickly.
    with pytest.raises(ValidationError, match="timed out"):
        wait_for_launcher_ready(
            prepared.run_directory,
            run_id=prepared.run_id,
            worker_token=token,
            timeout_seconds=0.2,
        )

    thread = threading.Thread(target=publisher)
    thread.start()
    # Publisher uses this test process pid, so wait from this process succeeds.
    record = wait_for_launcher_ready(
        prepared.run_directory,
        run_id=prepared.run_id,
        worker_token=token,
        timeout_seconds=2.0,
    )
    thread.join(timeout=2)
    assert record.worker_token == token
    assert record.pid == os.getpid()
    assert record.ready_at is not None
    persisted = read_launcher_record(prepared.run_directory)
    assert persisted is not None
    assert persisted.ready_at is not None


def test_worker_policy_mapping_never_asks() -> None:
    from ai_dev_loop.launch_worker import _policy_from_path

    policy = _policy_from_path(None)
    assert policy.update_mode == UpdateMode.NEVER
    assert policy.ask_callback is None

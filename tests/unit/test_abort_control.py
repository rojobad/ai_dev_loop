"""Unit tests for abort request and active-process control helpers."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

from ai_dev_loop.abort_control import (
    ACTIVE_PROCESS_REL_PATH,
    ActiveProcessComponent,
    ProcessSignalOutcome,
    ProcessSignalResult,
    abort_request_path,
    active_process_path,
    clear_active_process,
    is_stale_live_process_signal,
    read_abort_request,
    read_active_process,
    register_active_process,
    signal_active_process_group,
    signal_process_group,
    validate_active_process_metadata,
    write_abort_request,
)
from ai_dev_loop.paths import SENSITIVE_FILE_MODE

_FAKE_START = "1"
_FAKE_EXE = "/bin/false"


def test_write_and_read_abort_request(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    request = write_abort_request(run_directory, run_id="demo-run")
    assert request.run_id == "demo-run"
    assert request.reason == "user_requested_abort"

    loaded = read_abort_request(run_directory)
    assert loaded is not None
    assert loaded.run_id == "demo-run"
    assert abort_request_path(run_directory).is_file()


def test_abort_request_file_permissions(permission_test_root: Path) -> None:
    run_directory = permission_test_root / "run"
    run_directory.mkdir()
    write_abort_request(run_directory, run_id="demo-run")
    mode = stat.S_IMODE(abort_request_path(run_directory).stat().st_mode)
    assert mode == SENSITIVE_FILE_MODE


def test_register_active_process_redacts_prompt(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    metadata = register_active_process(
        run_directory,
        run_id="demo-run",
        component=ActiveProcessComponent.CURSOR,
        iteration=1,
        pid=1234,
        pgid=1234,
        parent_pid=os.getpid(),
        cwd=str(tmp_path),
        argv_redacted=["agent", "-p", "<prompt-redacted>"],
        process_start_time=_FAKE_START,
        executable=_FAKE_EXE,
    )
    assert metadata.argv_redacted[-1] == "<prompt-redacted>"
    loaded = read_active_process(run_directory)
    assert loaded is not None
    assert loaded.component == ActiveProcessComponent.CURSOR
    assert loaded.process_start_time == _FAKE_START
    assert loaded.executable == _FAKE_EXE
    assert (run_directory / ACTIVE_PROCESS_REL_PATH).is_file()


def test_validate_active_process_rejects_run_id_mismatch(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    register_active_process(
        run_directory,
        run_id="other-run",
        component="cursor",
        iteration=1,
        pid=os.getpid(),
        pgid=os.getpid(),
        parent_pid=os.getpid(),
        cwd=str(tmp_path),
        argv_redacted=["agent"],
    )
    validation = validate_active_process_metadata(run_directory, run_id="demo-run")
    assert validation is not None
    assert validation.is_stale is True
    assert validation.pgid_checked_live is False
    assert validation.stale_reason == "run_id mismatch"


def test_signal_stale_run_id_mismatch_preserves_live_metadata(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    try:
        register_active_process(
            run_directory,
            run_id="other-run",
            component="cursor",
            iteration=1,
            pid=proc.pid,
            pgid=pgid,
            parent_pid=os.getpid(),
            cwd=str(tmp_path),
            argv_redacted=["agent"],
        )
        result = signal_active_process_group(run_directory, run_id="demo-run")
        assert result.outcome == ProcessSignalOutcome.STALE
        assert active_process_path(run_directory).is_file()
    finally:
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=2)


def test_reused_pgid_with_stale_parent_lock_is_not_signaled(tmp_path: Path) -> None:
    """Live PGID + matching parent lock is insufficient without starttime/exe match."""

    from datetime import UTC, datetime

    from ai_dev_loop.locking import FileLock, LockMetadata, run_lock_path
    from ai_dev_loop.process import read_process_starttime

    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    lock = FileLock(run_lock_path(run_directory))
    lock.acquire(
        LockMetadata(
            pid=os.getpid(),
            run_id="demo-run",
            repository_path=str(tmp_path),
            started_at=datetime.now(tz=UTC),
        )
    )
    try:
        real_start = read_process_starttime(proc.pid)
        assert real_start is not None
        register_active_process(
            run_directory,
            run_id="demo-run",
            component="cursor",
            iteration=1,
            pid=proc.pid,
            pgid=pgid,
            parent_pid=os.getpid(),
            cwd=str(tmp_path),
            argv_redacted=["agent"],
            process_start_time=str(real_start + 999999),
            executable=str(Path(sys.executable).resolve()),
        )
        result = signal_active_process_group(run_directory, run_id="demo-run")
        assert result.outcome == ProcessSignalOutcome.STALE
        assert result.detail == "process start time mismatch"
        assert proc.poll() is None
    finally:
        lock.release()
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
        proc.wait(timeout=2)


def test_signal_process_group_terminates_child(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
        result = signal_process_group(pgid, grace_seconds=0.2)
        assert result.outcome == ProcessSignalOutcome.SIGNALED
        proc.wait(timeout=2)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=2)


def test_clear_active_process(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    register_active_process(
        run_directory,
        run_id="demo-run",
        component="codex",
        iteration=2,
        pid=1,
        pgid=1,
        parent_pid=os.getpid(),
        cwd=str(tmp_path),
        argv_redacted=["codex"],
        process_start_time=_FAKE_START,
        executable=_FAKE_EXE,
    )
    clear_active_process(run_directory)
    assert read_active_process(run_directory) is None


def test_abort_request_json_shape(tmp_path: Path) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    write_abort_request(run_directory, run_id="demo-run")
    payload = json.loads(abort_request_path(run_directory).read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["run_id"] == "demo-run"
    assert payload["reason"] == "user_requested_abort"
    assert "requested_at" in payload
    assert "requested_by_pid" in payload


def test_is_stale_live_process_signal(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        result = ProcessSignalResult(
            outcome=ProcessSignalOutcome.STALE,
            pgid=pgid,
            detail="parent workflow process is not clearly current",
        )
        assert is_stale_live_process_signal(result) is True
        proc.terminate()
        proc.wait(timeout=2)
        assert is_stale_live_process_signal(result) is False
    finally:
        if proc.poll() is None:
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)


def test_child_execution_aborted_requires_abort_request(tmp_path: Path) -> None:
    from ai_dev_loop.abort_control import child_execution_aborted

    run_directory = tmp_path / "run"
    run_directory.mkdir()
    assert child_execution_aborted(run_directory, returncode=-15, timed_out=False) is False
    write_abort_request(run_directory, run_id="demo-run")
    assert child_execution_aborted(run_directory, returncode=-15, timed_out=False) is True

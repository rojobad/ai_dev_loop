"""Direct subprocess execution helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import SENSITIVE_FILE_MODE, set_sensitive_file_mode


@dataclass(frozen=True)
class ActiveProcessRegistration:
    run_directory: Path
    run_id: str
    component: str
    iteration: int
    argv_redacted: list[str]


@dataclass(frozen=True)
class ProcessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class StreamingProcessResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_seconds: float


def _open_capture_file(path: Path, *, sensitive: bool) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sensitive and os.name != "nt":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SENSITIVE_FILE_MODE)
        return os.fdopen(fd, "w", encoding="utf-8")
    return path.open("w", encoding="utf-8")


@dataclass(frozen=True)
class BinaryProcessResult:
    """Byte-preserving subprocess result for binary-safe captures (for example Git diffs)."""

    args: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def run_process(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Run a subprocess in its own process group; terminate and reap on timeout."""

    try:
        proc = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise AiDevLoopError(f"executable not found: {args[0]}") from exc

    timed_out = False
    try:
        if timeout is None:
            stdout, stderr = proc.communicate()
        else:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
                stdout, stderr = proc.communicate()
    except Exception:
        _terminate_process_group(proc)
        raise

    returncode = proc.returncode if proc.returncode is not None else (124 if timed_out else 1)
    return ProcessResult(
        args=list(args),
        returncode=returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
    )


def run_process_bytes(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> BinaryProcessResult:
    """Run a subprocess capturing stdout/stderr as raw bytes (no text decoding)."""

    try:
        proc = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise AiDevLoopError(f"executable not found: {args[0]}") from exc

    timed_out = False
    try:
        if timeout is None:
            stdout, stderr = proc.communicate()
        else:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
                stdout, stderr = proc.communicate()
    except Exception:
        _terminate_process_group(proc)
        raise

    returncode = proc.returncode if proc.returncode is not None else (124 if timed_out else 1)
    return BinaryProcessResult(
        args=list(args),
        returncode=returncode,
        stdout=stdout or b"",
        stderr=stderr or b"",
        timed_out=timed_out,
    )


def run_process_streaming(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    sensitive: bool = False,
    active_process: ActiveProcessRegistration | None = None,
) -> StreamingProcessResult:
    """Run a subprocess in its own process group with optional artifact capture."""
    start = time.monotonic()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_handle = None
    stderr_handle = None

    try:
        if stdout_path is not None:
            stdout_handle = _open_capture_file(stdout_path, sensitive=sensitive)
        if stderr_path is not None:
            stderr_handle = _open_capture_file(stderr_path, sensitive=sensitive)

        proc = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()
        raise AiDevLoopError(f"executable not found: {args[0]}") from exc

    registration_recorded = False
    if active_process is not None:
        from ai_dev_loop.abort_control import register_active_process

        try:
            register_active_process(
                active_process.run_directory,
                run_id=active_process.run_id,
                component=active_process.component,
                iteration=active_process.iteration,
                pid=proc.pid,
                pgid=os.getpgid(proc.pid),
                parent_pid=os.getpid(),
                cwd=cwd or os.getcwd(),
                argv_redacted=list(active_process.argv_redacted),
            )
            registration_recorded = True
        except Exception as exc:
            _terminate_process_group(proc)
            try:
                proc.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)
                proc.communicate(timeout=1)
            if stdout_handle is not None:
                stdout_handle.close()
            if stderr_handle is not None:
                stderr_handle.close()
            raise AiDevLoopError(
                "failed to register active child process metadata; child process group terminated"
            ) from exc

    timed_out = False
    returncode = 1
    clear_reason = "completed"
    try:
        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            if timeout is None:
                stdout_data, stderr_data = proc.communicate(input=stdin_text)
            else:
                stdout_data, stderr_data = proc.communicate(input=stdin_text, timeout=timeout)
            stdout_chunks.append(stdout_data)
            stderr_chunks.append(stderr_data)
            returncode = proc.returncode if proc.returncode is not None else 0
        except subprocess.TimeoutExpired:
            timed_out = True
            clear_reason = "timed_out"
            _terminate_process_group(proc)
            stdout_data, stderr_data = proc.communicate()
            stdout_chunks.append(stdout_data)
            stderr_chunks.append(stderr_data)
            returncode = proc.returncode if proc.returncode is not None else 124
    finally:
        if active_process is not None and registration_recorded:
            from ai_dev_loop.abort_control import is_abort_requested, mark_active_process_cleared

            if is_abort_requested(active_process.run_directory):
                clear_reason = "aborted"
            mark_active_process_cleared(active_process.run_directory, reason=clear_reason)
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        if stdout_handle is not None:
            stdout_handle.write(stdout_text)
            stdout_handle.close()
            if sensitive and stdout_path is not None:
                set_sensitive_file_mode(stdout_path)
        if stderr_handle is not None:
            stderr_handle.write(stderr_text)
            stderr_handle.close()
            if sensitive and stderr_path is not None:
                set_sensitive_file_mode(stderr_path)

    elapsed = time.monotonic() - start
    return StreamingProcessResult(
        args=list(args),
        returncode=returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        timed_out=timed_out,
        elapsed_seconds=elapsed,
    )


def _terminate_process_group(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        if proc.poll() is not None:
            return
        time.sleep(0.1)


def require_success(result: ProcessResult, *, context: str) -> str:
    if result.timed_out:
        raise AiDevLoopError(f"{context}: command timed out: {' '.join(result.args)}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise AiDevLoopError(f"{context}: {' '.join(result.args)} failed: {detail}")
    return result.stdout.strip()

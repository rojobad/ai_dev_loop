"""Direct subprocess execution helpers."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import SENSITIVE_FILE_MODE, set_sensitive_file_mode


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


def run_process(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    try:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode()
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode()
        return ProcessResult(
            args=list(args),
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        raise AiDevLoopError(f"executable not found: {args[0]}") from exc

    return ProcessResult(
        args=list(args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_process_streaming(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    sensitive: bool = False,
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

    timed_out = False
    returncode = 1
    try:
        assert proc.stdout is not None
        assert proc.stderr is not None
        if timeout is None:
            stdout_data, stderr_data = proc.communicate()
            stdout_chunks.append(stdout_data)
            stderr_chunks.append(stderr_data)
            returncode = proc.wait()
        else:
            deadline = start + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate_process_group(proc)
                    stdout_data, stderr_data = proc.communicate()
                    stdout_chunks.append(stdout_data)
                    stderr_chunks.append(stderr_data)
                    returncode = proc.returncode if proc.returncode is not None else 124
                    break
                try:
                    stdout_data, stderr_data = proc.communicate(timeout=min(0.2, remaining))
                    stdout_chunks.append(stdout_data)
                    stderr_chunks.append(stderr_data)
                    returncode = proc.wait()
                    break
                except subprocess.TimeoutExpired:
                    continue
    finally:
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


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
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

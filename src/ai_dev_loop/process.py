"""Direct subprocess execution helpers."""

from __future__ import annotations

import codecs
import contextlib
import errno
import fcntl
import os
import select
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import SENSITIVE_FILE_MODE, set_sensitive_file_mode


def read_process_starttime(pid: int) -> int | None:
    """Return Linux ``/proc/<pid>/stat`` starttime, or None when unavailable."""

    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def read_process_pgid(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


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


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _select_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 0.05
    return min(0.05, max(0.0, deadline - time.monotonic()))


class _BoundedTextCapture:
    """Incrementally decode UTF-8 subprocess output without splitting multibyte characters."""

    def __init__(
        self,
        chunks: list[str],
        handle: IO[str] | None,
        limit: int | None,
    ) -> None:
        self._chunks = chunks
        self._handle = handle
        self._limit = limit
        self._total = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def append(self, piece: bytes) -> bool:
        if not piece:
            return False
        new_total = self._total + len(piece)
        if self._limit is not None and new_total > self._limit:
            return True
        self._total = new_total
        text = self._decoder.decode(piece, final=False)
        self._emit(text)
        return False

    def finalize(self) -> bool:
        try:
            text = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            text = "\ufffd"
        self._emit(text)
        return False

    def _emit(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        if self._handle is not None:
            self._handle.write(text)


def _read_nonblocking(fd: int) -> tuple[bytes, bool]:
    try:
        piece = os.read(fd, 4096)
    except BlockingIOError:
        return b"", False
    if not piece:
        return b"", True
    return piece, False


def _write_nonblocking(fd: int, payload: bytes, offset: int) -> tuple[int, bool]:
    if offset >= len(payload):
        return offset, False
    try:
        sent = os.write(fd, payload[offset:])
    except BlockingIOError:
        return offset, False
    except BrokenPipeError:
        return offset, True
    except OSError as exc:
        if exc.errno == errno.EPIPE:
            return offset, True
        raise
    return offset + sent, False


def _capture_bounded_streams(
    proc: subprocess.Popen[Any],
    *,
    stdin_text: str | None,
    timeout: float | None,
    max_stdout_bytes: int | None,
    max_stderr_bytes: int | None,
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    stdout_handle: IO[str] | None,
    stderr_handle: IO[str] | None,
) -> tuple[bool, bool]:
    """Multiplex stdin/stdout/stderr under one deadline with byte limits and EOF draining."""

    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    _set_nonblocking(stdout_fd)
    _set_nonblocking(stderr_fd)
    stdin_fd: int | None = None
    stdin_payload = stdin_text.encode("utf-8") if stdin_text else b""
    stdin_offset = 0
    stdin_closed = proc.stdin is None or not stdin_payload
    if proc.stdin is not None and stdin_payload:
        stdin_fd = proc.stdin.fileno()
        _set_nonblocking(stdin_fd)
    elif proc.stdin is not None:
        proc.stdin.close()
        stdin_closed = True

    deadline = None if timeout is None else time.monotonic() + timeout
    stdout_eof = False
    stderr_eof = False
    timed_out = False
    overflow = False
    stdout_capture = _BoundedTextCapture(stdout_chunks, stdout_handle, max_stdout_bytes)
    stderr_capture = _BoundedTextCapture(stderr_chunks, stderr_handle, max_stderr_bytes)

    def _close_stdin_input() -> None:
        nonlocal stdin_closed, stdin_fd
        stdin_pipe = proc.stdin
        if stdin_pipe is not None:
            with contextlib.suppress(OSError):
                stdin_pipe.close()
        stdin_closed = True
        stdin_fd = None

    def _consume_stdout(piece: bytes, *, eof: bool) -> bool:
        nonlocal stdout_eof, overflow
        if eof:
            stdout_eof = True
            if stdout_capture.finalize():
                overflow = True
                return True
            return False
        if not piece:
            return False
        if stdout_capture.append(piece):
            overflow = True
            return True
        return False

    def _consume_stderr(piece: bytes, *, eof: bool) -> bool:
        nonlocal stderr_eof, overflow
        if eof:
            stderr_eof = True
            if stderr_capture.finalize():
                overflow = True
                return True
            return False
        if not piece:
            return False
        if stderr_capture.append(piece):
            overflow = True
            return True
        return False

    def _drain_available() -> bool:
        while not stdout_eof:
            piece, eof = _read_nonblocking(stdout_fd)
            if _consume_stdout(piece, eof=eof):
                return True
            if eof or not piece:
                break
        while not stderr_eof:
            piece, eof = _read_nonblocking(stderr_fd)
            if _consume_stderr(piece, eof=eof):
                return True
            if eof or not piece:
                break
        return False

    try:
        while True:
            if deadline is not None and time.monotonic() > deadline:
                timed_out = True
                break
            if proc.poll() is not None and stdout_eof and stderr_eof and stdin_closed:
                break

            read_fds: list[int] = []
            write_fds: list[int] = []
            if not stdout_eof:
                read_fds.append(stdout_fd)
            if not stderr_eof:
                read_fds.append(stderr_fd)
            if stdin_fd is not None and not stdin_closed:
                write_fds.append(stdin_fd)

            if read_fds or write_fds:
                readable, writable, _ = select.select(
                    read_fds,
                    write_fds,
                    [],
                    _select_timeout(deadline),
                )
                for fd in readable:
                    if fd == stdout_fd:
                        while not stdout_eof:
                            piece, eof = _read_nonblocking(stdout_fd)
                            if _consume_stdout(piece, eof=eof):
                                break
                            if eof or not piece:
                                break
                    elif fd == stderr_fd:
                        while not stderr_eof:
                            piece, eof = _read_nonblocking(stderr_fd)
                            if _consume_stderr(piece, eof=eof):
                                break
                            if eof or not piece:
                                break
                if overflow:
                    break
                for fd in writable:
                    if fd == stdin_fd and stdin_fd is not None and not stdin_closed:
                        stdin_offset, pipe_closed = _write_nonblocking(
                            stdin_fd, stdin_payload, stdin_offset
                        )
                        if pipe_closed or stdin_offset >= len(stdin_payload):
                            _close_stdin_input()
            elif proc.poll() is not None:
                if _drain_available():
                    break
                if stdout_eof and stderr_eof:
                    break
            else:
                time.sleep(_select_timeout(deadline))

            if overflow:
                break

        if not timed_out and not overflow:
            while not (stdout_eof and stderr_eof):
                if deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                    break
                if _drain_available():
                    break
                if stdout_eof and stderr_eof:
                    break
                time.sleep(_select_timeout(deadline))
    except Exception:
        _terminate_process_group(proc)
        raise

    if timed_out or overflow:
        _terminate_process_group(proc)
    elif not stdin_closed:
        _close_stdin_input()

    return timed_out, overflow


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
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
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
        from ai_dev_loop.errors import ValidationError

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
            # Fast-exiting children (for example create-chat) may leave /proc before
            # executable/starttime capture. If the child is already reaped/exited,
            # there is nothing left to abort-control — continue without metadata.
            if isinstance(exc, ValidationError) and proc.poll() is not None:
                registration_recorded = False
            else:
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
    output_overflow = False
    handles_already_written = False
    returncode = 1
    clear_reason = "completed"
    try:
        assert proc.stdout is not None
        assert proc.stderr is not None

        if max_stdout_bytes is not None or max_stderr_bytes is not None:
            handles_already_written = stdout_handle is not None or stderr_handle is not None
            try:
                timed_out, output_overflow = _capture_bounded_streams(
                    proc,
                    stdin_text=stdin_text,
                    timeout=timeout,
                    max_stdout_bytes=max_stdout_bytes,
                    max_stderr_bytes=max_stderr_bytes,
                    stdout_chunks=stdout_chunks,
                    stderr_chunks=stderr_chunks,
                    stdout_handle=stdout_handle,
                    stderr_handle=stderr_handle,
                )
            except Exception:
                if proc.poll() is None:
                    _terminate_process_group(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(proc)
                    proc.wait(timeout=5)
                raise
            if timed_out:
                clear_reason = "timed_out"
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_process_group(proc)
                proc.wait(timeout=5)
            returncode = (
                proc.returncode if proc.returncode is not None else (124 if timed_out else 1)
            )
            if output_overflow:
                returncode = 2
        else:
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
            if not handles_already_written:
                stdout_handle.write(stdout_text)
            stdout_handle.close()
            if sensitive and stdout_path is not None:
                set_sensitive_file_mode(stdout_path)
        if stderr_handle is not None:
            if not handles_already_written:
                stderr_handle.write(stderr_text)
            stderr_handle.close()
            if sensitive and stderr_path is not None:
                set_sensitive_file_mode(stderr_path)

    elapsed = time.monotonic() - start
    return StreamingProcessResult(
        args=list(args),
        returncode=returncode if not output_overflow else 2,
        stdout=stdout_text,
        stderr=stderr_text,
        timed_out=timed_out or output_overflow,
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

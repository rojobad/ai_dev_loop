"""Unit tests for subprocess helpers."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import ai_dev_loop.process as process_module
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.process import (
    ActiveProcessRegistration,
    _BoundedTextCapture,
    run_process_streaming,
)


def test_process_streaming_accepts_stdin_text(tmp_path: Path) -> None:
    script = tmp_path / "read_stdin.py"
    script.write_text(
        "import sys\nprint(sys.stdin.read().strip())\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        stdin_text="hello from stdin",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "hello from stdin"


def test_process_streaming_times_out_while_writing_stdin(tmp_path: Path) -> None:
    script = tmp_path / "sleep_without_read.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    large_prompt = "x" * 131_072

    start = time.monotonic()
    result = run_process_streaming(
        [sys.executable, str(script)],
        stdin_text=large_prompt,
        timeout=0.5,
    )
    elapsed = time.monotonic() - start

    assert result.timed_out is True
    assert elapsed < 5.0


def test_process_streaming_timeout_does_not_duplicate_output(tmp_path: Path) -> None:
    script = tmp_path / "print_then_sleep.py"
    script.write_text(
        "import sys, time\nprint('before', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        timeout=0.5,
    )
    assert result.timed_out is True
    assert result.stdout == "before\n"


def test_sensitive_capture_files_are_restrictive_while_process_runs(
    permission_test_root: Path,
    tmp_path: Path,
) -> None:
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")

    stdout_path = permission_test_root / "events.jsonl"
    stderr_path = permission_test_root / "stderr.txt"
    observed: dict[str, int | None] = {"stdout_mode": None, "stderr_mode": None}
    done = threading.Event()

    def run_streaming() -> None:
        run_process_streaming(
            [sys.executable, str(sleeper)],
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            sensitive=True,
        )
        done.set()

    thread = threading.Thread(target=run_streaming)
    thread.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if stdout_path.is_file() and stderr_path.is_file():
                observed["stdout_mode"] = stat.S_IMODE(stdout_path.stat().st_mode)
                observed["stderr_mode"] = stat.S_IMODE(stderr_path.stat().st_mode)
                break
            time.sleep(0.05)
        assert observed["stdout_mode"] == SENSITIVE_FILE_MODE
        assert observed["stderr_mode"] == SENSITIVE_FILE_MODE
    finally:
        done.wait(timeout=3)
        thread.join(timeout=3)

    assert stdout_path.is_file()
    assert stderr_path.is_file()
    assert stat.S_IMODE(stdout_path.stat().st_mode) == SENSITIVE_FILE_MODE
    assert stat.S_IMODE(stderr_path.stat().st_mode) == SENSITIVE_FILE_MODE


def test_registration_failure_terminates_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    tracked: dict[str, subprocess.Popen[str]] = {}
    original_popen = subprocess.Popen

    def track_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        tracked["proc"] = proc
        return proc

    monkeypatch.setattr(subprocess, "Popen", track_popen)

    def fail_register(*args, **kwargs):
        raise OSError("registration failed")

    with (
        patch(
            "ai_dev_loop.abort_control.register_active_process",
            side_effect=fail_register,
        ),
        pytest.raises(AiDevLoopError, match="register active child process metadata"),
    ):
        run_process_streaming(
            [sys.executable, str(script)],
            active_process=ActiveProcessRegistration(
                run_directory=run_directory,
                run_id="demo-run",
                component="cursor",
                iteration=1,
                argv_redacted=["agent", "<prompt-redacted>"],
            ),
        )

    proc = tracked["proc"]
    proc.wait(timeout=2)
    assert proc.returncode is not None


def test_bounded_streaming_times_out_after_partial_output_then_stall(tmp_path: Path) -> None:
    script = tmp_path / "partial_then_stall.py"
    script.write_text(
        "import sys, time\nprint('partial', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    start = time.monotonic()
    result = run_process_streaming(
        [sys.executable, str(script)],
        timeout=0.5,
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert elapsed < 5.0
    assert result.stdout == "partial\n"
    assert result.returncode != 0


def test_bounded_streaming_times_out_on_slow_stdin_consumption(tmp_path: Path) -> None:
    script = tmp_path / "slow_stdin_reader.py"
    script.write_text(
        "import sys, time\n"
        "while True:\n"
        "    chunk = sys.stdin.read(1024)\n"
        "    if not chunk:\n"
        "        break\n"
        "    time.sleep(0.2)\n",
        encoding="utf-8",
    )
    start = time.monotonic()
    result = run_process_streaming(
        [sys.executable, str(script)],
        stdin_text="x" * 16_384,
        timeout=0.5,
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert elapsed < 5.0


def test_bounded_streaming_captures_simultaneous_stdout_stderr(tmp_path: Path) -> None:
    script = tmp_path / "both_streams.py"
    script.write_text(
        "import sys\nsys.stdout.write('stdout-bytes')\nsys.stderr.write('stderr-bytes')\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.stdout == "stdout-bytes"
    assert result.stderr == "stderr-bytes"


def test_bounded_streaming_drains_fast_exiting_child_both_streams(tmp_path: Path) -> None:
    script = tmp_path / "fast_both.py"
    script.write_text(
        "import sys\nsys.stdout.write('A' * 8192)\nsys.stderr.write('B' * 4096)\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert len(result.stdout.encode("utf-8")) == 8192
    assert len(result.stderr.encode("utf-8")) == 4096


def test_bounded_streaming_handles_early_stdin_closure_and_reaps_child(tmp_path: Path) -> None:
    script = tmp_path / "close_stdin_early.py"
    script.write_text(
        "import os, sys, time\n"
        "print(f'pid:{os.getpid()}', file=sys.stderr, flush=True)\n"
        "print('stdin-closed-diagnostic', file=sys.stderr, flush=True)\n"
        "sys.stdin.close()\n"
        "time.sleep(0.3)\n",
        encoding="utf-8",
    )
    large_prompt = "p" * 131_072
    result = run_process_streaming(
        [sys.executable, str(script)],
        stdin_text=large_prompt,
        timeout=5.0,
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
    )
    assert result.timed_out is False
    assert "stdin-closed-diagnostic" in result.stderr
    pid_line = next(line for line in result.stderr.splitlines() if line.startswith("pid:"))
    child_pid = int(pid_line.split(":", 1)[1])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_bounded_text_capture_decodes_multibyte_sequences_across_chunks() -> None:
    expected = "Hello 世界 café"
    payload = expected.encode("utf-8")
    stdout_chunks: list[str] = []
    capture = _BoundedTextCapture(stdout_chunks, None, 256 * 1024)
    for byte in payload:
        assert capture.append(bytes([byte])) is False
    capture.finalize()
    assert "".join(stdout_chunks) == expected


def test_bounded_streaming_preserves_utf8_across_read_boundaries(tmp_path: Path) -> None:
    expected = "Hello 世界 café"
    script = tmp_path / "emit_unicode.py"
    script.write_text(
        f"import sys\nsys.stdout.write({expected!r})\nsys.stderr.write({expected!r})\n",
        encoding="utf-8",
    )
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    real_read = process_module._read_nonblocking
    pending: dict[int, bytes] = {}

    def fragmented_read(fd: int) -> tuple[bytes, bool]:
        if pending.get(fd):
            piece = pending[fd][:1]
            pending[fd] = pending[fd][1:]
            return piece, False
        piece, eof = real_read(fd)
        if len(piece) > 1:
            pending[fd] = piece[1:]
            return piece[:1], False
        return piece, eof

    with patch.object(process_module, "_read_nonblocking", side_effect=fragmented_read):
        result = run_process_streaming(
            [sys.executable, str(script)],
            max_stdout_bytes=256 * 1024,
            max_stderr_bytes=256 * 1024,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.stdout == expected
    assert result.stderr == expected
    assert stdout_path.read_text(encoding="utf-8") == expected
    assert stderr_path.read_text(encoding="utf-8") == expected


def test_bounded_streaming_terminates_on_stdout_limit_without_timeout(tmp_path: Path) -> None:
    script = tmp_path / "huge_stdout.py"
    script.write_text(
        "import sys\nsys.stdout.write('x' * 500000)\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
    )
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert result.returncode == 2
    assert result.stdout_captured_bytes <= 256 * 1024


def test_bounded_streaming_drain_after_limit_preserves_exit_code(tmp_path: Path) -> None:
    script = tmp_path / "huge_then_exit.py"
    script.write_text(
        "import sys\nsys.stdout.write('y' * 500000)\nprint('done', file=sys.stderr)\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
        drain_after_limit=True,
    )
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert result.returncode == 0
    assert result.stderr == "done\n"
    assert result.stdout_captured_bytes <= 256 * 1024


def test_bounded_streaming_drain_after_limit_still_times_out(tmp_path: Path) -> None:
    script = tmp_path / "overflow_then_stall.py"
    script.write_text(
        "import sys, time\nsys.stdout.write('z' * 500000)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    start = time.monotonic()
    result = run_process_streaming(
        [sys.executable, str(script)],
        timeout=0.5,
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=256 * 1024,
        drain_after_limit=True,
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert result.stdout_truncated is True
    assert elapsed < 5.0


def test_bounded_streaming_retains_partial_chunk_at_exact_limit(tmp_path: Path) -> None:
    payload = "a" * 260
    script = tmp_path / "exact_limit.py"
    script.write_text(
        f"import sys\nsys.stdout.buffer.write({payload.encode('utf-8')!r})\n",
        encoding="utf-8",
    )
    result = run_process_streaming(
        [sys.executable, str(script)],
        max_stdout_bytes=256,
        max_stderr_bytes=256 * 1024,
        drain_after_limit=True,
    )
    assert result.returncode == 0
    assert result.stdout_truncated is True
    assert result.stdout_captured_bytes == 256
    assert result.stdout == "a" * 256


def test_bounded_streaming_terminates_child_on_unexpected_capture_exception(
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "child.pid"
    script = tmp_path / "sleepy.py"
    script.write_text(
        f"import os, sys, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stderr.write('awake\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    with (
        patch.object(
            process_module,
            "_read_nonblocking",
            side_effect=RuntimeError("capture failure"),
        ),
        pytest.raises(RuntimeError, match="capture failure"),
    ):
        run_process_streaming(
            [sys.executable, str(script)],
            timeout=5.0,
            max_stdout_bytes=256 * 1024,
            max_stderr_bytes=256 * 1024,
        )
    for _ in range(20):
        if pid_path.exists():
            break
        time.sleep(0.01)
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)

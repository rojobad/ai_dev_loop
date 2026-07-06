"""Unit tests for subprocess helpers."""

from __future__ import annotations

import stat
import sys
import threading
import time
from pathlib import Path

from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.process import run_process_streaming


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

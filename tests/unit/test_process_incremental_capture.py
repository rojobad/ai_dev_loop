"""Regression tests for incremental file capture visibility (Phase 21.5)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from ai_dev_loop.process import run_process_streaming


def test_incremental_capture_flushes_before_child_exit(tmp_path: Path) -> None:
    gate = tmp_path / "release"
    script = tmp_path / "gate_child.py"
    script.write_text(
        f"""
import sys, time, os
sys.stdout.write("LIVE-SENTINEL")
sys.stdout.flush()
ready = {repr(str(gate))}
while not os.path.exists(ready):
    time.sleep(0.01)
time.sleep(0.05)
""",
        encoding="utf-8",
    )
    stdout_path = tmp_path / "events.jsonl"
    done = threading.Event()

    def run() -> None:
        run_process_streaming(
            [sys.executable, str(script)],
            stdout_path=stdout_path,
            incremental_file_capture=True,
        )
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if stdout_path.is_file() and b"LIVE-SENTINEL" in stdout_path.read_bytes():
                gate.write_text("go", encoding="utf-8")
                break
            time.sleep(0.02)
        done.wait(timeout=5)
    finally:
        if not gate.is_file():
            gate.write_text("go", encoding="utf-8")
        thread.join(timeout=5)
    assert stdout_path.read_bytes() == b"LIVE-SENTINEL"


def test_incremental_capture_large_stdin_non_reading_child_times_out(tmp_path: Path) -> None:
    script = tmp_path / "ignore_stdin.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    start = time.monotonic()
    result = run_process_streaming(
        [sys.executable, str(script)],
        stdin_text="x" * 131_072,
        timeout=0.5,
        incremental_file_capture=True,
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert elapsed < 5.0


def test_incremental_capture_large_stdin_reading_child_completes(tmp_path: Path) -> None:
    script = tmp_path / "read_stdin.py"
    script.write_text(
        "import sys\npayload = sys.stdin.read()\nprint(len(payload))\n",
        encoding="utf-8",
    )
    payload = "y" * 65536
    result = run_process_streaming(
        [sys.executable, str(script)],
        stdin_text=payload,
        timeout=5.0,
        incremental_file_capture=True,
    )
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout.strip() == str(len(payload))

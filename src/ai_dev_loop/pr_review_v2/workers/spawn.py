"""Detached supervisor process launcher for PR review v2."""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from ai_dev_loop.launcher import read_process_starttime
from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SystemClock
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
    validate_launcher_ownership_against_os,
)

WORKER_MODULE = "ai_dev_loop.pr_review_v2_supervisor_worker"


def _terminate_and_reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError):
            pgid = None
        if pgid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pgid, sig)
                except (OSError, ProcessLookupError):
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        else:
            with contextlib.suppress(OSError, ProcessLookupError):
                proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=1)


def _read_process_executable(pid: int) -> str:
    try:
        return str(Path(f"/proc/{pid}/exe").resolve())
    except OSError:
        return str(Path(sys.executable).resolve())


def spawn_detached_supervisor(
    run_id: str,
    *,
    artifact_root: Path,
    launcher_store: SupervisorLauncherStore,
) -> str:
    """Spawn or reuse the owned v2 supervisor for ``run_id``.

    Returns ``spawned``, ``reused``, or ``spawn_failed``. Never claims ``spawned``
    without durable launcher metadata for a live owned process group.
    """

    existing = launcher_store.read(run_id)
    if existing is not None and validate_launcher_ownership_against_os(existing, run_id=run_id):
        return "reused"
    if existing is not None:
        launcher_store.clear(run_id)

    token = secrets.token_hex(16)
    run_root = ensure_run_artifact_root(artifact_root, run_id)
    log_dir = ensure_dir(run_root / "logs", mode=DIR_MODE)
    stdout_path = log_dir / "supervisor.stdout.txt"
    stderr_path = log_dir / "supervisor.stderr.txt"
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    set_sensitive_file_mode(stdout_path)
    set_sensitive_file_mode(stderr_path)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULE, run_id, token],
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            cwd=str(run_root),
        )
    except OSError:
        return "spawn_failed"
    finally:
        stdout_handle.close()
        stderr_handle.close()

    try:
        if proc.pid <= 0:
            raise RuntimeError("invalid supervisor pid")
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError) as exc:
            raise RuntimeError("failed to resolve supervisor pgid") from exc
        starttime = read_process_starttime(proc.pid)
        if starttime is None:
            raise RuntimeError("failed to capture supervisor process start time")
        executable = _read_process_executable(proc.pid)
        metadata = SupervisorLauncherMetadata(
            schema_version=1,
            run_id=run_id,
            token=token,
            pid=proc.pid,
            pgid=pgid,
            process_start_time=str(starttime),
            executable=executable,
            created_at=SystemClock().now().isoformat(),
        )
        launcher_store.write(metadata)
    except Exception:
        _terminate_and_reap(proc)
        with contextlib.suppress(Exception):
            launcher_store.clear(run_id)
        return "spawn_failed"
    return "spawned"


__all__ = ["WORKER_MODULE", "spawn_detached_supervisor"]

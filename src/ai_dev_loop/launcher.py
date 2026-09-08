"""Detached local worker launcher metadata and process identity checks."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.locking import FileLock, LockMetadata, is_process_alive
from ai_dev_loop.state import atomic_write_json

LAUNCHER_REL_PATH = Path("locks/launcher.json")
LAUNCHER_LOCK_REL_PATH = Path("locks/launch.lock")
LAUNCHER_POLICY_REL_PATH = Path("locks/launcher-policy.json")
LAUNCHER_STDOUT_REL_PATH = Path("logs/launcher.stdout.txt")
LAUNCHER_STDERR_REL_PATH = Path("logs/launcher.stderr.txt")

WORKER_READY_TIMEOUT_SECONDS = 30.0
WORKER_READY_POLL_SECONDS = 0.05
_CODEX_WSL_STATE_ENV_KEYS = ("CODEX_HOME", "CODEX_SQLITE_HOME")


class LauncherOutcome(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class LauncherRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    run_id: str
    worker_token: str
    pid: int
    pgid: int
    parent_pid: int
    started_at: datetime
    pid_starttime: int | None = None
    ready_at: datetime | None = None
    argv_redacted: list[str]
    stdout_path: str
    stderr_path: str
    outcome: LauncherOutcome = LauncherOutcome.RUNNING
    finished_at: datetime | None = None
    exit_code: int | None = None
    workflow_status: str | None = None
    safe_error: str | None = None


@dataclass(frozen=True)
class LauncherValidation:
    record: LauncherRecord
    is_live: bool
    is_stale: bool
    stale_reason: str | None = None


@dataclass(frozen=True)
class SerializedLaunchPolicy:
    """Non-interactive tool policy carried into the detached worker."""

    update_mode: str  # "always" | "never"
    allow_incompatible: bool = False


def detached_worker_environment(
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Keep detached Codex work on native WSL state, not inherited DrvFS state.

    Codex Desktop may propagate Windows ``CODEX_HOME`` values into WSL. A
    detached fresh reviewer must use the native CLI's authentication, SQLite
    state, configuration, and sessions instead. Native WSL overrides remain
    supported for users who deliberately configure them.
    """

    environment = dict(os.environ if base_environment is None else base_environment)
    for key in _CODEX_WSL_STATE_ENV_KEYS:
        value = environment.get(key, "").strip()
        if value.startswith("/mnt/"):
            environment.pop(key, None)
    return environment


def launcher_path(run_directory: Path) -> Path:
    return run_directory / LAUNCHER_REL_PATH


def launch_lock_path(run_directory: Path) -> Path:
    return run_directory / LAUNCHER_LOCK_REL_PATH


def launcher_policy_path(run_directory: Path) -> Path:
    return run_directory / LAUNCHER_POLICY_REL_PATH


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


def process_identity_matches(record: LauncherRecord) -> tuple[bool, str | None]:
    """Verify the recorded worker is still the same process, not a reused PID."""

    if record.pid <= 0 or record.pgid <= 0:
        return False, "launcher pid/pgid invalid"
    if not is_process_alive(record.pid):
        return False, "launcher pid not live"
    current_pgid = read_process_pgid(record.pid)
    if current_pgid is None:
        return False, "launcher pgid unavailable"
    if current_pgid != record.pgid:
        return False, "launcher pgid mismatch"
    if record.pid_starttime is not None:
        current_starttime = read_process_starttime(record.pid)
        if current_starttime is None:
            return False, "launcher starttime unavailable"
        if current_starttime != record.pid_starttime:
            return False, "launcher starttime mismatch"
    return True, None


def read_launcher_record(run_directory: Path) -> LauncherRecord | None:
    path = launcher_path(run_directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LauncherRecord.model_validate(data)
    except (OSError, ValueError, TypeError):
        return None


def write_launcher_record(run_directory: Path, record: LauncherRecord) -> None:
    payload = record.model_dump(mode="json", by_alias=True)
    payload["started_at"] = record.started_at.isoformat()
    if record.ready_at is not None:
        payload["ready_at"] = record.ready_at.isoformat()
    if record.finished_at is not None:
        payload["finished_at"] = record.finished_at.isoformat()
    atomic_write_json(launcher_path(run_directory), payload, sensitive=True)


def write_launcher_policy(run_directory: Path, policy: SerializedLaunchPolicy) -> Path:
    path = launcher_policy_path(run_directory)
    if policy.update_mode not in {"always", "never"}:
        raise ValidationError(
            f"detached launch policy update_mode must be always or never, got {policy.update_mode}"
        )
    atomic_write_json(
        path,
        {
            "update_mode": policy.update_mode,
            "allow_incompatible": policy.allow_incompatible,
        },
        sensitive=True,
    )
    return path


def read_launcher_policy(path: Path) -> SerializedLaunchPolicy:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mode = str(data["update_mode"])
        if mode not in {"always", "never"}:
            raise ValidationError("launcher policy update_mode must be always or never")
        return SerializedLaunchPolicy(
            update_mode=mode,
            allow_incompatible=bool(data.get("allow_incompatible", False)),
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"invalid launcher policy artifact: {path}") from exc


def validate_launcher_record(
    run_directory: Path,
    *,
    run_id: str,
) -> LauncherValidation | None:
    record = read_launcher_record(run_directory)
    if record is None:
        return None
    if record.run_id != run_id:
        return LauncherValidation(
            record=record,
            is_live=False,
            is_stale=True,
            stale_reason="launcher run_id mismatch",
        )
    if record.pid <= 0 or record.pgid <= 0:
        return LauncherValidation(
            record=record,
            is_live=False,
            is_stale=True,
            stale_reason="launcher pid/pgid invalid",
        )
    identity_ok, identity_reason = process_identity_matches(record)
    if record.outcome == LauncherOutcome.RUNNING and not identity_ok:
        return LauncherValidation(
            record=record,
            is_live=False,
            is_stale=True,
            stale_reason=identity_reason or "launcher identity stale",
        )
    if record.outcome != LauncherOutcome.RUNNING and identity_ok:
        return LauncherValidation(
            record=record,
            is_live=True,
            is_stale=True,
            stale_reason="launcher marked finished but process identity still live",
        )
    return LauncherValidation(
        record=record,
        is_live=identity_ok and record.outcome == LauncherOutcome.RUNNING,
        is_stale=False,
    )


def launcher_summary(run_directory: Path, *, run_id: str) -> dict[str, Any]:
    validation = validate_launcher_record(run_directory, run_id=run_id)
    if validation is None:
        return {
            "launcher_registered": False,
            "launcher_live": False,
            "launcher_stale": False,
            "launcher_outcome": None,
            "launcher_pid": None,
            "launcher_pgid": None,
            "stale_reason": None,
        }
    record = validation.record
    return {
        "launcher_registered": True,
        "launcher_live": validation.is_live,
        "launcher_stale": validation.is_stale,
        "launcher_outcome": record.outcome.value,
        "launcher_pid": record.pid if validation.is_live else None,
        "launcher_pgid": record.pgid if validation.is_live else None,
        "stale_reason": validation.stale_reason,
        "workflow_status": record.workflow_status,
        "safe_error": record.safe_error,
    }


def worker_acknowledged_ready(record: LauncherRecord) -> bool:
    """True once the worker has consumed the published launcher record."""

    return record.ready_at is not None or record.outcome != LauncherOutcome.RUNNING


def acknowledge_launcher_ready(
    run_directory: Path,
    *,
    run_id: str,
    worker_token: str,
) -> LauncherRecord:
    """Persist a token/PID-bound readiness acknowledgement for the current worker."""

    existing = read_launcher_record(run_directory)
    if (
        existing is None
        or existing.run_id != run_id
        or existing.worker_token != worker_token
        or existing.pid != os.getpid()
    ):
        raise ValidationError(
            "cannot acknowledge launcher readiness without a matching launcher record"
        )
    if existing.ready_at is not None:
        return existing
    updated = existing.model_copy(update={"ready_at": datetime.now(tz=UTC)})
    write_launcher_record(run_directory, updated)
    return updated


def wait_for_launcher_ready(
    run_directory: Path,
    *,
    run_id: str,
    worker_token: str,
    timeout_seconds: float = WORKER_READY_TIMEOUT_SECONDS,
) -> LauncherRecord:
    """Block until the parent has published a matching launcher record for this worker."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        record = read_launcher_record(run_directory)
        if (
            record is not None
            and record.run_id == run_id
            and record.worker_token == worker_token
            and record.pid == os.getpid()
        ):
            return acknowledge_launcher_ready(
                run_directory,
                run_id=run_id,
                worker_token=worker_token,
            )
        time.sleep(WORKER_READY_POLL_SECONDS)
    raise ValidationError(
        "detached worker timed out waiting for launcher metadata; "
        f"inspect {LAUNCHER_STDERR_REL_PATH}"
    )


def mark_launcher_finished(
    run_directory: Path,
    *,
    run_id: str,
    worker_token: str,
    pid: int,
    exit_code: int,
    workflow_status: str | None,
    safe_error: str | None,
) -> bool:
    """Finalize the launcher record only when run_id, worker_token, and pid match.

    Returns True when the record was updated.
    """

    existing = read_launcher_record(run_directory)
    if existing is None:
        return False
    if existing.run_id != run_id or existing.worker_token != worker_token or existing.pid != pid:
        return False
    if workflow_status == "aborted":
        outcome = LauncherOutcome.ABORTED
    elif exit_code == 0:
        outcome = LauncherOutcome.COMPLETED
    else:
        outcome = LauncherOutcome.FAILED
    updated = existing.model_copy(
        update={
            "outcome": outcome,
            "finished_at": datetime.now(tz=UTC),
            "exit_code": exit_code,
            "workflow_status": workflow_status,
            "safe_error": safe_error,
        }
    )
    write_launcher_record(run_directory, updated)
    return True


def mark_launcher_preready_failure(
    run_directory: Path,
    *,
    run_id: str,
    worker_token: str,
    pid: int,
    exit_code: int,
    safe_error: str,
) -> bool:
    """Mark failure only when the worker never acknowledged readiness.

    Returns False when the worker already acknowledged or finalized the record,
    so a fast-successful worker is not overwritten.
    """

    existing = read_launcher_record(run_directory)
    if existing is None:
        return False
    if existing.run_id != run_id or existing.worker_token != worker_token or existing.pid != pid:
        return False
    if worker_acknowledged_ready(existing):
        return False
    return mark_launcher_finished(
        run_directory,
        run_id=run_id,
        worker_token=worker_token,
        pid=pid,
        exit_code=exit_code,
        workflow_status=None,
        safe_error=safe_error,
    )


def spawn_detached_worker(
    run_directory: Path,
    *,
    run_id: str,
    policy: SerializedLaunchPolicy | None = None,
) -> LauncherRecord:
    """Spawn a detached worker after the caller holds the launch lock.

    The worker waits for this function to publish ``launcher.json`` before it
    may call ``start_run``.
    """

    stdout_path = run_directory / LAUNCHER_STDOUT_REL_PATH
    stderr_path = run_directory / LAUNCHER_STDERR_REL_PATH
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    worker_token = secrets.token_hex(16)
    argv = [
        sys.executable,
        "-m",
        "ai_dev_loop.launch_worker",
        run_id,
        worker_token,
    ]
    argv_redacted = [
        sys.executable,
        "-m",
        "ai_dev_loop.launch_worker",
        "<run-id>",
        "<worker-token>",
    ]
    if policy is not None:
        policy_path = write_launcher_policy(run_directory, policy)
        argv.append(str(policy_path))
        argv_redacted.append(str(LAUNCHER_POLICY_REL_PATH))

    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv array, shell=False
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            cwd=str(run_directory),
            env=detached_worker_environment(),
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid

    pid_starttime = read_process_starttime(proc.pid)
    record = LauncherRecord(
        run_id=run_id,
        worker_token=worker_token,
        pid=proc.pid,
        pgid=pgid,
        parent_pid=os.getpid(),
        started_at=datetime.now(tz=UTC),
        pid_starttime=pid_starttime,
        argv_redacted=argv_redacted,
        stdout_path=str(LAUNCHER_STDOUT_REL_PATH),
        stderr_path=str(LAUNCHER_STDERR_REL_PATH),
        outcome=LauncherOutcome.RUNNING,
    )
    try:
        write_launcher_record(run_directory, record)
    except Exception:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        raise

    # Wait briefly for either readiness acknowledgement or a true pre-ready exit.
    for _ in range(20):
        current = read_launcher_record(run_directory)
        if current is not None and worker_acknowledged_ready(current):
            return current
        if proc.poll() is not None:
            break
        time.sleep(0.01)

    current = read_launcher_record(run_directory)
    if current is not None and worker_acknowledged_ready(current):
        return current

    identity_ok, identity_reason = process_identity_matches(record)
    if proc.poll() is not None or not identity_ok:
        safe_error = (
            "detached launcher exited before becoming ready; "
            f"inspect {LAUNCHER_STDERR_REL_PATH}"
            + (f" ({identity_reason})" if identity_reason else "")
        )
        marked = mark_launcher_preready_failure(
            run_directory,
            run_id=run_id,
            worker_token=worker_token,
            pid=proc.pid,
            exit_code=1 if proc.returncode is None else int(proc.returncode),
            safe_error=safe_error,
        )
        if not marked:
            # Worker acknowledged or finalized while we were checking.
            final = read_launcher_record(run_directory)
            if final is not None and worker_acknowledged_ready(final):
                return final
        raise ValidationError(
            "detached launcher exited before launcher metadata was observed by the worker; "
            f"inspect {LAUNCHER_STDERR_REL_PATH}"
        )
    return record


class LaunchLock:
    """Exclusive lock covering launch validation, spawn, and record publication.

    Uses ``fcntl`` flock for cross-process exclusion and a per-path threading
    lock so concurrent in-process callers (and tests) also serialize. Waiters
    block with timeout so the second caller observes the published live worker
    instead of racing a second spawn.
    """

    _thread_locks_guard = threading.Lock()
    _thread_locks: dict[str, threading.Lock] = {}

    def __init__(
        self,
        run_directory: Path,
        metadata: LockMetadata,
        *,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._path = launch_lock_path(run_directory)
        self._lock = FileLock(self._path)
        self._metadata = metadata
        self._timeout_seconds = timeout_seconds
        self._thread_lock = self._thread_lock_for(self._path)
        self._thread_acquired = False

    @classmethod
    def _thread_lock_for(cls, path: Path) -> threading.Lock:
        key = str(path)
        with cls._thread_locks_guard:
            existing = cls._thread_locks.get(key)
            if existing is None:
                existing = threading.Lock()
                cls._thread_locks[key] = existing
            return existing

    def acquire(self) -> None:
        from ai_dev_loop.errors import LockError

        deadline = time.monotonic() + self._timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LockError(
                    f"timed out waiting for launch lock for run {self._metadata.run_id}"
                )
            if self._thread_lock.acquire(timeout=min(remaining, WORKER_READY_POLL_SECONDS)):
                self._thread_acquired = True
                try:
                    self._lock.acquire(self._metadata)
                except LockError as exc:
                    self._thread_lock.release()
                    self._thread_acquired = False
                    if time.monotonic() >= deadline:
                        raise LockError(
                            f"timed out waiting for launch lock for run {self._metadata.run_id}"
                        ) from exc
                    time.sleep(WORKER_READY_POLL_SECONDS)
                    continue
                return

    def release(self) -> None:
        self._lock.release()
        if self._thread_acquired:
            self._thread_lock.release()
            self._thread_acquired = False

    def __enter__(self) -> LaunchLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        self.release()

"""Dedicated PR review v2 supervisor loop and launcher metadata."""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_dev_loop.launcher import read_process_pgid, read_process_starttime
from ai_dev_loop.locking import is_process_alive
from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.application.contracts import Clock, WorkerStepResult
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain.state import (
    WaitingForUserState,
    pending_deferred_reply_dispatch,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SystemClock
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

TERMINAL_KINDS = frozenset({"completed", "failed", "aborted"})
PAUSE_EXIT_KINDS = frozenset({"paused"})
LAUNCHER_RELATIVE = "local/supervisor-launcher.json"


@dataclass(frozen=True)
class SupervisorLauncherMetadata:
    schema_version: int
    run_id: str
    token: str
    pid: int
    pgid: int
    process_start_time: str
    executable: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "token": self.token,
            "pid": self.pid,
            "pgid": self.pgid,
            "process_start_time": self.process_start_time,
            "executable": self.executable,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SupervisorLauncherMetadata:
        return cls(
            schema_version=int(payload["schema_version"]),
            run_id=str(payload["run_id"]),
            token=str(payload["token"]),
            pid=int(payload["pid"]),
            pgid=int(payload["pgid"]),
            process_start_time=str(payload["process_start_time"]),
            executable=str(payload["executable"]),
            created_at=str(payload["created_at"]),
        )


class SupervisorLauncherStore:
    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root

    def path_for(self, run_id: str) -> Path:
        from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root

        return ensure_run_artifact_root(self._root, run_id) / LAUNCHER_RELATIVE

    def write(self, metadata: SupervisorLauncherMetadata) -> Path:
        path = self.path_for(metadata.run_id)
        ensure_dir(path.parent, mode=DIR_MODE)
        payload = json.dumps(
            metadata.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        set_sensitive_file_mode(tmp)
        os.replace(tmp, path)
        set_sensitive_file_mode(path)
        return path

    def read(self, run_id: str) -> SupervisorLauncherMetadata | None:
        path = self.path_for(run_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return SupervisorLauncherMetadata.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def clear(self, run_id: str) -> None:
        path = self.path_for(run_id)
        if path.is_file():
            path.unlink()


def validate_launcher_ownership(
    metadata: SupervisorLauncherMetadata,
    *,
    run_id: str,
    expected_token: str | None = None,
) -> bool:
    """Structural ownership checks before any OS signaling."""

    if metadata.schema_version != 1:
        return False
    if metadata.run_id != run_id:
        return False
    if not metadata.token or not metadata.token.strip():
        return False
    if expected_token is not None and metadata.token != expected_token:
        return False
    if metadata.pid <= 0 or metadata.pgid <= 0:
        return False
    if not metadata.process_start_time.strip():
        return False
    try:
        int(metadata.process_start_time)
    except ValueError:
        return False
    return bool(metadata.executable.strip())


def validate_launcher_ownership_against_os(
    metadata: SupervisorLauncherMetadata,
    *,
    run_id: str,
    expected_token: str | None = None,
) -> bool:
    """Full ownership check including live process identity."""

    if not validate_launcher_ownership(metadata, run_id=run_id, expected_token=expected_token):
        return False
    if not is_process_alive(metadata.pid):
        return False
    current_pgid = read_process_pgid(metadata.pid)
    if current_pgid is None or current_pgid != metadata.pgid:
        return False
    current_start = read_process_starttime(metadata.pid)
    if current_start is None:
        return False
    try:
        recorded_start = int(metadata.process_start_time)
    except ValueError:
        return False
    if current_start != recorded_start:
        return False
    exe = _read_process_executable(metadata.pid)
    if exe is None:
        return False
    return Path(exe).resolve() == Path(metadata.executable).resolve()


def _read_process_executable(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        return str(Path(f"/proc/{pid}/exe").resolve())
    except OSError:
        return None


def process_group_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class PrReviewV2Supervisor:
    """Process one effect at a time for a single run; exit on pause/terminal."""

    def __init__(
        self,
        engine: PrReviewEngine,
        worker: EffectWorker,
        *,
        run_id: str,
        idle_poll_seconds: float = 1.0,
        clock: Clock | None = None,
        max_steps: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._engine = engine
        self._worker = worker
        self._run_id = run_id
        self._idle_poll_seconds = idle_poll_seconds
        self._clock = clock or SystemClock()
        self._max_steps = max_steps
        self._steps = 0
        self._cancel_check = cancel_check or (lambda: False)
        self._sleep = sleep or time.sleep

    def _should_exit_waiting_for_user(self, run_id: str) -> bool:
        """Exit only when operator continuation is required, not during reply dispatch."""

        with self._engine.store.begin_read() as conn:
            state, _, _ = self._engine.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, WaitingForUserState):
            return True
        return pending_deferred_reply_dispatch(state) is None

    def run_until_idle(self) -> str:
        """Run until terminal, paused, waiting-for-user, or no claimable work.

        Returns the durable state kind at exit. Future ``waiting_retry`` timers keep
        the supervisor alive with bounded interruptible waits (no SQLite txn/lease).
        """

        while True:
            if self._cancel_check():
                status = self._engine.get_status(self._run_id)
                return status.state_kind
            if self._max_steps is not None and self._steps >= self._max_steps:
                status = self._engine.get_status(self._run_id)
                return status.state_kind
            self._engine.fire_due_timers_for_run(self._run_id)
            status = self._engine.get_status(self._run_id)
            if status.state_kind in TERMINAL_KINDS:
                return status.state_kind
            if status.state_kind == "waiting_for_user" and self._should_exit_waiting_for_user(
                self._run_id
            ):
                return status.state_kind
            if status.state_kind == "paused":
                return status.state_kind
            if status.state_kind == "prepared":
                return status.state_kind
            if (
                status.state_kind == "waiting_retry"
                and status.next_eligible_at is not None
                and status.next_eligible_at > self._clock.now()
            ):
                remaining = (status.next_eligible_at - self._clock.now()).total_seconds()
                wait = min(max(remaining, 0.0), self._idle_poll_seconds)
                self._sleep(wait)
                continue

            result: WorkerStepResult = self._worker.run_once(self._run_id)
            self._steps += 1
            if not result.claimed:
                # Idle wait without holding leases/transactions.
                self._sleep(self._idle_poll_seconds)
                status = self._engine.get_status(self._run_id)
                if status.state_kind in TERMINAL_KINDS | PAUSE_EXIT_KINDS | {"prepared"}:
                    return status.state_kind
                if status.state_kind == "waiting_for_user" and self._should_exit_waiting_for_user(
                    self._run_id
                ):
                    return status.state_kind
                if status.state_kind == "waiting_retry":
                    continue
                if not status.lease_active and status.active_effect_kind is None:
                    return status.state_kind
                continue


def signal_owned_supervisor(
    metadata: SupervisorLauncherMetadata,
    *,
    run_id: str,
    grace_seconds: float = 2.0,
) -> str:
    """Terminate an exactly validated supervisor process group.

    Returns ``terminated``, ``unnecessary``, or ``refused``.
    """

    if not validate_launcher_ownership_against_os(metadata, run_id=run_id):
        # Structural match without live OS identity → refuse signaling.
        if validate_launcher_ownership(metadata, run_id=run_id) and (
            not is_process_alive(metadata.pid) and not process_group_alive(metadata.pgid)
        ):
            return "unnecessary"
        return "refused"
    try:
        os.killpg(metadata.pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "unnecessary"
    except PermissionError:
        return "refused"
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_group_alive(metadata.pgid):
            return "terminated"
        time.sleep(0.05)
    try:
        os.killpg(metadata.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "refused"
    return "terminated"


__all__ = [
    "LAUNCHER_RELATIVE",
    "PrReviewV2Supervisor",
    "SupervisorLauncherMetadata",
    "SupervisorLauncherStore",
    "process_group_alive",
    "signal_owned_supervisor",
    "validate_launcher_ownership",
    "validate_launcher_ownership_against_os",
]

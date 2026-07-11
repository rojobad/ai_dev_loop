"""Abort request markers, active child-process metadata, and signaling helpers."""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.locking import is_process_alive, read_lock_metadata, run_lock_path
from ai_dev_loop.state import atomic_write_json

ABORT_REQUEST_REL_PATH = Path("locks/abort-request.json")
ACTIVE_PROCESS_REL_PATH = Path("locks/active-process.json")

ABORT_REASON_USER_REQUEST = "user_requested_abort"
DEFAULT_SIGNAL_GRACE_SECONDS = 0.5


class ActiveProcessComponent(StrEnum):
    CURSOR = "cursor"
    CODEX = "codex"
    CURSOR_UPDATE = "cursor_update"
    CODEX_UPDATE = "codex_update"


class AbortRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    run_id: str
    requested_at: datetime
    requested_by_pid: int
    reason: str = ABORT_REASON_USER_REQUEST


class ActiveProcess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    run_id: str
    component: ActiveProcessComponent
    iteration: int
    pid: int
    pgid: int
    parent_pid: int
    started_at: datetime
    cwd: str
    argv_redacted: list[str]


class ActiveProcessValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: ActiveProcess
    is_live: bool
    is_stale: bool
    pgid_checked_live: bool = False
    stale_reason: str | None = None


class ProcessSignalOutcome(StrEnum):
    SIGNALED = "signaled"
    NOT_LIVE = "not_live"
    STALE = "stale"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ProcessSignalResult:
    outcome: ProcessSignalOutcome
    pgid: int | None = None
    detail: str | None = None


def abort_request_path(run_directory: Path) -> Path:
    return run_directory / ABORT_REQUEST_REL_PATH


def active_process_path(run_directory: Path) -> Path:
    return run_directory / ACTIVE_PROCESS_REL_PATH


def write_abort_request(run_directory: Path, *, run_id: str) -> AbortRequest:
    request = AbortRequest(
        run_id=run_id,
        requested_at=datetime.now(tz=UTC),
        requested_by_pid=os.getpid(),
        reason=ABORT_REASON_USER_REQUEST,
    )
    payload = request.model_dump(mode="json", by_alias=True)
    payload["requested_at"] = request.requested_at.isoformat()
    atomic_write_json(abort_request_path(run_directory), payload, sensitive=True)
    return request


def read_abort_request(run_directory: Path) -> AbortRequest | None:
    path = abort_request_path(run_directory)
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return AbortRequest.model_validate(data)
    except (OSError, ValueError, TypeError):
        return None


def is_abort_requested(run_directory: Path) -> bool:
    return read_abort_request(run_directory) is not None


def register_active_process(
    run_directory: Path,
    *,
    run_id: str,
    component: ActiveProcessComponent | str,
    iteration: int,
    pid: int,
    pgid: int,
    parent_pid: int,
    cwd: str,
    argv_redacted: list[str],
) -> ActiveProcess:
    component_value = (
        component
        if isinstance(component, ActiveProcessComponent)
        else ActiveProcessComponent(str(component))
    )
    metadata = ActiveProcess(
        run_id=run_id,
        component=component_value,
        iteration=iteration,
        pid=pid,
        pgid=pgid,
        parent_pid=parent_pid,
        started_at=datetime.now(tz=UTC),
        cwd=cwd,
        argv_redacted=list(argv_redacted),
    )
    payload = metadata.model_dump(mode="json", by_alias=True)
    payload["started_at"] = metadata.started_at.isoformat()
    payload["component"] = metadata.component.value
    atomic_write_json(active_process_path(run_directory), payload, sensitive=True)
    return metadata


def read_active_process(run_directory: Path) -> ActiveProcess | None:
    path = active_process_path(run_directory)
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return ActiveProcess.model_validate(data)
    except (OSError, ValueError, TypeError):
        return None


def clear_active_process(run_directory: Path) -> None:
    path = active_process_path(run_directory)
    if path.is_file():
        path.unlink(missing_ok=True)


def mark_active_process_cleared(run_directory: Path, *, reason: str) -> None:
    metadata = read_active_process(run_directory)
    if metadata is None:
        return
    payload: dict[str, Any] = metadata.model_dump(mode="json", by_alias=True)
    payload["started_at"] = metadata.started_at.isoformat()
    payload["component"] = metadata.component.value
    payload["cleared_at"] = datetime.now(tz=UTC).isoformat()
    payload["cleared_reason"] = reason
    atomic_write_json(active_process_path(run_directory), payload, sensitive=True)


def is_process_group_alive(pgid: int) -> bool:
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def validate_active_process_metadata(
    run_directory: Path,
    *,
    run_id: str,
    metadata: ActiveProcess | None = None,
) -> ActiveProcessValidation | None:
    active = metadata if metadata is not None else read_active_process(run_directory)
    if active is None:
        return None
    if active.run_id != run_id:
        return ActiveProcessValidation(
            metadata=active,
            is_live=False,
            is_stale=True,
            pgid_checked_live=False,
            stale_reason="run_id mismatch",
        )
    if active.pid <= 0 or active.pgid <= 0:
        return ActiveProcessValidation(
            metadata=active,
            is_live=False,
            is_stale=True,
            pgid_checked_live=False,
            stale_reason="invalid pid or pgid",
        )

    live = is_process_group_alive(active.pgid)
    if not live:
        return ActiveProcessValidation(
            metadata=active,
            is_live=False,
            is_stale=True,
            pgid_checked_live=True,
            stale_reason="process group is not live",
        )

    lock_metadata = read_lock_metadata(run_lock_path(run_directory))
    parent_live = is_process_alive(active.parent_pid)
    lock_matches = lock_metadata is not None and lock_metadata.pid == active.parent_pid
    if not parent_live and not lock_matches:
        return ActiveProcessValidation(
            metadata=active,
            is_live=True,
            is_stale=True,
            pgid_checked_live=True,
            stale_reason="parent workflow process is not clearly current",
        )
    return ActiveProcessValidation(
        metadata=active,
        is_live=True,
        is_stale=False,
        pgid_checked_live=True,
    )


def signal_active_process_group(
    run_directory: Path,
    *,
    run_id: str,
    grace_seconds: float = DEFAULT_SIGNAL_GRACE_SECONDS,
) -> ProcessSignalResult:
    validation = validate_active_process_metadata(run_directory, run_id=run_id)
    if validation is None:
        return ProcessSignalResult(
            outcome=ProcessSignalOutcome.SKIPPED,
            detail="no active process metadata",
        )
    if validation.is_stale:
        if validation.pgid_checked_live and not validation.is_live:
            clear_active_process(run_directory)
        return ProcessSignalResult(
            outcome=ProcessSignalOutcome.STALE,
            pgid=validation.metadata.pgid,
            detail=validation.stale_reason,
        )
    return signal_process_group(
        validation.metadata.pgid,
        grace_seconds=grace_seconds,
    )


def signal_process_group(
    pgid: int,
    *,
    grace_seconds: float = DEFAULT_SIGNAL_GRACE_SECONDS,
) -> ProcessSignalResult:
    if pgid <= 0:
        return ProcessSignalResult(
            outcome=ProcessSignalOutcome.STALE,
            detail="invalid process group id",
        )
    if not is_process_group_alive(pgid):
        return ProcessSignalResult(
            outcome=ProcessSignalOutcome.NOT_LIVE,
            pgid=pgid,
            detail="process group is not live",
        )
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return ProcessSignalResult(
            outcome=ProcessSignalOutcome.NOT_LIVE,
            pgid=pgid,
            detail="process group exited before SIGTERM",
        )
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not is_process_group_alive(pgid):
            return ProcessSignalResult(outcome=ProcessSignalOutcome.SIGNALED, pgid=pgid)
        time.sleep(0.05)
    if not is_process_group_alive(pgid):
        return ProcessSignalResult(outcome=ProcessSignalOutcome.SIGNALED, pgid=pgid)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return ProcessSignalResult(
            outcome=ProcessSignalOutcome.SIGNALED,
            pgid=pgid,
            detail="process group exited before SIGKILL",
        )
    return ProcessSignalResult(outcome=ProcessSignalOutcome.SIGNALED, pgid=pgid)


def is_run_lock_held_by_live_process(run_directory: Path) -> bool:
    metadata = read_lock_metadata(run_lock_path(run_directory))
    if metadata is None:
        return False
    return is_process_alive(metadata.pid)


def child_terminated_by_abort_signal(returncode: int) -> bool:
    return returncode < 0 and returncode in {-signal.SIGTERM, -signal.SIGKILL}


def is_stale_live_process_signal(signal_result: ProcessSignalResult) -> bool:
    """Return True when signaling was skipped as stale but the pgid is still live."""
    if signal_result.outcome != ProcessSignalOutcome.STALE:
        return False
    if signal_result.pgid is None:
        return False
    return is_process_group_alive(signal_result.pgid)


def abort_control_summary(run_directory: Path) -> dict[str, Any]:
    request = read_abort_request(run_directory)
    active = read_active_process(run_directory)
    summary: dict[str, Any] = {
        "abort_requested": request is not None,
        "active_process_registered": active is not None,
    }
    if request is not None:
        summary["abort_requested_at"] = request.requested_at.isoformat()
        summary["abort_reason"] = request.reason
    if active is not None:
        summary["active_component"] = active.component.value
        summary["active_iteration"] = active.iteration
        summary["active_pgid"] = active.pgid
    return summary


def require_non_terminal_abort_target(status_value: str) -> None:
    if status_value in {
        "completed",
        "completed_with_residual_risk",
        "max_iterations_reached",
        "failed",
        "aborted",
    }:
        raise ValidationError(f"run is in terminal status {status_value}; abort is not available")

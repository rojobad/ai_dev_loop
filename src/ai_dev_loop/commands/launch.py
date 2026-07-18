"""Launch or resume an eligible A/B run via a detached local worker."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.launcher import (
    LaunchLock,
    SerializedLaunchPolicy,
    spawn_detached_worker,
    validate_launcher_record,
)
from ai_dev_loop.locking import LockMetadata
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.runners.tool_updates import ToolUpdateFlags, validate_tool_update_flags
from ai_dev_loop.state import RunState, RunStatus, shorten_session_id


@dataclass(frozen=True)
class LaunchResult:
    run_id: str
    status: str
    already_running: bool
    launcher_pid: int | None
    launcher_pgid: int | None
    message: str
    controller_session_id: str
    reviewer_session_id: str


def _serialized_policy_from_flags(flags: ToolUpdateFlags) -> SerializedLaunchPolicy:
    """Build a non-interactive policy for the detached worker.

    Launch never prompts. ``--update-tools`` becomes always; otherwise never.
    """

    validate_tool_update_flags(flags)
    mode = "always" if flags.update_tools else "never"
    return SerializedLaunchPolicy(
        update_mode=mode,
        allow_incompatible=flags.allow_incompatible_tools,
    )


def _validate_controller_identity(
    state: RunState,
    *,
    controller_id: str,
    repo_path: Path | None,
) -> None:
    if state.controller is None:
        raise ValidationError(
            "run was not prepared with --controller-session-id; "
            "use ai_dev_loop start for legacy runs"
        )
    if state.controller.controller_session_id != controller_id:
        raise ValidationError(
            "controller session id does not match the prepared run; "
            "refusing launch with mismatched controller identity"
        )
    if state.codex.session_id == controller_id:
        raise ValidationError("controller session id must differ from the reviewer session id")
    if state.status in {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        RunStatus.MAX_ITERATIONS_REACHED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    }:
        raise ValidationError(f"cannot launch terminal run in status {state.status.value}")
    if repo_path is not None:
        repo_info = discover_repository(repo_path)
        if Path(state.repository.root).resolve() != repo_info.root.resolve():
            raise ValidationError("repository path does not match the prepared run repository root")


def launch_run(
    run_id: str,
    *,
    controller_session_id: str,
    repo_path: Path | None = None,
    tool_flags: ToolUpdateFlags | None = None,
) -> LaunchResult:
    controller_id = require_codex_session_id(controller_session_id)
    run_directory, state = load_run(run_id)
    flags = tool_flags or ToolUpdateFlags()
    policy = _serialized_policy_from_flags(flags)

    # Cheap pre-lock rejection for obvious mismatches; authoritative checks happen
    # again inside LaunchLock after a fresh state reload.
    _validate_controller_identity(state, controller_id=controller_id, repo_path=repo_path)

    lock_metadata = LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=state.repository.root,
        started_at=datetime.now(tz=UTC),
    )
    with LaunchLock(run_directory, lock_metadata):
        run_directory, state = load_run(run_id)
        _validate_controller_identity(state, controller_id=controller_id, repo_path=repo_path)

        existing = validate_launcher_record(run_directory, run_id=run_id)
        if existing is not None and existing.is_live and not existing.is_stale:
            return LaunchResult(
                run_id=run_id,
                status=state.status.value,
                already_running=True,
                launcher_pid=existing.record.pid,
                launcher_pgid=existing.record.pgid,
                message=(
                    "Detached worker already running for this run. "
                    "Leave the reviewer session inactive."
                ),
                controller_session_id=controller_id,
                reviewer_session_id=state.codex.session_id,
            )

        if state.status not in {RunStatus.PREPARED, RunStatus.WAITING_FOR_CURSOR_FIX}:
            raise ValidationError(
                "launch requires status prepared or waiting_for_cursor_fix "
                f"(current: {state.status.value}); wait for the existing worker or inspect artifacts"
            )
        record = spawn_detached_worker(run_directory, run_id=run_id, policy=policy)

    return LaunchResult(
        run_id=run_id,
        status=state.status.value,
        already_running=False,
        launcher_pid=record.pid,
        launcher_pgid=record.pgid,
        message=(
            "Detached worker started. Leave the reviewer Codex session inactive "
            "while the run is active. Use controller status or abort from the "
            "controller session only."
        ),
        controller_session_id=controller_id,
        reviewer_session_id=state.codex.session_id,
    )


def render_launch_output(result: LaunchResult, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "run_id": result.run_id,
            "status": result.status,
            "already_running": result.already_running,
            "launcher_live": result.launcher_pid is not None,
            "message": result.message,
            "controller_session_id_prefix": result.controller_session_id[:8],
            "reviewer_session_id_prefix": result.reviewer_session_id[:8],
        }
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        f"Launch: {result.run_id}",
        f"Status: {result.status}",
        f"Already running: {result.already_running}",
        f"Controller: {shorten_session_id(result.controller_session_id)}",
        f"Reviewer: {shorten_session_id(result.reviewer_session_id)}",
        result.message,
    ]
    return "\n".join(lines) + "\n"

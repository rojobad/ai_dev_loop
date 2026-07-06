"""Abort command: request workflow cancellation and signal active child processes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_dev_loop.abort_control import (
    ProcessSignalOutcome,
    is_run_lock_held_by_live_process,
    is_stale_live_process_signal,
    require_non_terminal_abort_target,
    signal_active_process_group,
    write_abort_request,
)
from ai_dev_loop.commands.start_preflight import mark_aborted
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.event_log import EventLevel, append_orchestrator_event
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, append_run_log, save_run_state
from ai_dev_loop.workflow_engine import ABORT_RESULT_MESSAGE

STALE_LIVE_PROCESS_MESSAGE = (
    "Abort request recorded, but active-process metadata is stale while the recorded "
    "process group is still live. The run was not marked aborted; inspect "
    "locks/active-process.json and terminate the process manually if needed."
)


@dataclass(frozen=True)
class AbortResult:
    run_id: str
    status: str
    abort_requested: bool
    active_process_signaled: bool
    signal_outcome: str | None
    signal_detail: str | None
    message: str


def abort_run(run_id: str) -> AbortResult:
    run_directory, state = load_run(run_id)
    require_non_terminal_abort_target(state.status.value)

    write_abort_request(run_directory, run_id=run_id)
    append_run_log(run_directory, f"abort requested for run {run_id}")
    append_orchestrator_event(
        run_directory,
        run_id=run_id,
        component="orchestrator",
        event="abort_requested",
        status=state.status.value,
        detail={"requested_by_pid": os.getpid()},
    )

    signal_result = signal_active_process_group(run_directory, run_id=run_id)
    active_process_signaled = signal_result.outcome == ProcessSignalOutcome.SIGNALED
    if signal_result.outcome == ProcessSignalOutcome.SIGNALED:
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="active_process_signaled",
            status=state.status.value,
            detail={
                "pgid": signal_result.pgid,
                "outcome": signal_result.outcome.value,
            },
        )
    elif signal_result.outcome == ProcessSignalOutcome.STALE:
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="abort_stale_process_metadata",
            level=EventLevel.WARNING,
            status=state.status.value,
            detail={
                "pgid": signal_result.pgid,
                "reason": signal_result.detail,
            },
        )
    elif signal_result.outcome == ProcessSignalOutcome.NOT_LIVE:
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="abort_no_active_process",
            status=state.status.value,
            detail={"reason": signal_result.detail},
        )

    workflow_lock_held = is_run_lock_held_by_live_process(run_directory)
    stale_live_process = is_stale_live_process_signal(signal_result)
    final_status = state.status.value
    message: str

    if stale_live_process:
        message = STALE_LIVE_PROCESS_MESSAGE
    elif active_process_signaled or workflow_lock_held:
        message = (
            "Abort requested. The active workflow will stop at the next safe checkpoint "
            "after the child process exits."
        )
        if active_process_signaled:
            message = (
                "Abort requested and active child process group signaled. "
                "The workflow will mark the run aborted after the child exits."
            )
    else:
        metadata = _lock_metadata(run_id, state)
        with RunLocks(run_directory, metadata):
            _, state = load_run(run_id)
            if state.status in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
                RunStatus.MAX_ITERATIONS_REACHED,
                RunStatus.FAILED,
                RunStatus.ABORTED,
            }:
                message = f"Run is already in terminal status {state.status.value}."
                final_status = state.status.value
            else:
                mark_aborted(state, ABORT_RESULT_MESSAGE)
                save_run_state(run_directory, state)
                append_run_log(run_directory, ABORT_RESULT_MESSAGE)
                append_orchestrator_event(
                    run_directory,
                    run_id=run_id,
                    component="orchestrator",
                    event="workflow_aborted",
                    status=state.status.value,
                    detail={"message": ABORT_RESULT_MESSAGE},
                )
                final_status = state.status.value
                message = ABORT_RESULT_MESSAGE

    return AbortResult(
        run_id=run_id,
        status=final_status,
        abort_requested=True,
        active_process_signaled=active_process_signaled,
        signal_outcome=signal_result.outcome.value if signal_result.outcome else None,
        signal_detail=signal_result.detail,
        message=message,
    )


def abort_terminal_run(run_id: str) -> AbortResult:
    """Return a refusal result for terminal runs without mutating state."""
    run_directory, state = load_run(run_id)
    message = f"Run is in terminal status {state.status.value}; abort is not available."
    append_orchestrator_event(
        run_directory,
        run_id=run_id,
        component="orchestrator",
        event="abort_refused_terminal_state",
        level=EventLevel.WARNING,
        status=state.status.value,
    )
    return AbortResult(
        run_id=run_id,
        status=state.status.value,
        abort_requested=False,
        active_process_signaled=False,
        signal_outcome=None,
        signal_detail=None,
        message=message,
    )


def run_abort(run_id: str) -> AbortResult:
    run_directory, state = load_run(run_id)
    if state.status in {
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        RunStatus.MAX_ITERATIONS_REACHED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    }:
        return abort_terminal_run(run_id)
    try:
        return abort_run(run_id)
    except ValidationError:
        return abort_terminal_run(run_id)


def render_abort_output(result: AbortResult) -> str:
    lines = [
        f"Run {result.run_id}",
        f"Status: {result.status}",
        f"Abort requested: {'yes' if result.abort_requested else 'no'}",
    ]
    if result.active_process_signaled:
        lines.append("Active child process: signaled")
    elif result.signal_outcome == ProcessSignalOutcome.STALE.value:
        lines.append("Active child process: stale metadata (not signaled)")
    elif result.signal_outcome == ProcessSignalOutcome.NOT_LIVE.value:
        lines.append("Active child process: not live")
    elif result.signal_outcome == ProcessSignalOutcome.SKIPPED.value:
        lines.append("Active child process: none")
    lines.append(result.message)
    return "\n".join(lines) + "\n"


def _lock_metadata(run_id: str, state) -> LockMetadata:  # type: ignore[no-untyped-def]
    return LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=state.repository.root,
        started_at=datetime.now(tz=UTC),
    )

"""Read-only controller status lookup by exact controller session identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.scheduler.application.contracts import (
    ControllerSchedulerCandidate,
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    scheduler_abort_safe_next_action,
)
from ai_dev_loop.scheduler.application.controller_read import (
    find_scheduler_candidates,
    load_scheduler_candidate,
)
from ai_dev_loop.scheduler.domain.state import SCHEDULER_ABORTABLE_STATE_KINDS


@dataclass(frozen=True)
class ControllerStatusResult:
    match_count: int
    run_id: str | None
    status: str | None
    project: str | None
    repository: str | None
    current_review_iteration: int | None
    max_review_iterations: int | None
    next_safe_action: str
    last_error: str | None
    result: str | None
    abort_control: dict[str, object] | None
    controller_session_id: str
    reviewer_session_id_prefix: str | None
    candidate_run_ids: list[str]
    scheduler_state_kind: str | None = None
    capacity_holder_run_id_prefix: str | None = None
    last_event_kind: str | None = None
    cursor_wait_until: str | None = None
    block_reason_kind: str | None = None
    run_source: str | None = None
    read_failure: str | None = None


def controller_status(
    *,
    controller_session_id: str,
    repo_path: Path,
    run_id: str | None = None,
    include_terminal: bool = False,
) -> ControllerStatusResult:
    controller_id = require_codex_session_id(controller_session_id)
    repo_info = discover_repository(repo_path)
    repo_root = repo_info.root.resolve()

    if run_id is not None:
        scheduler_candidate, scheduler_read_failure = _load_scheduler_candidate_or_failure(
            run_id=run_id,
            controller_id=controller_id,
            repo_root=repo_root,
        )
        if scheduler_read_failure is not None:
            return _read_failure_result(
                controller_id=controller_id,
                repo_root=repo_root,
                message=scheduler_read_failure,
            )
        if scheduler_candidate is None:
            return _empty_controller_result(controller_id=controller_id, repo_root=repo_root)
        return _build_scheduler_result(
            candidate=scheduler_candidate,
            controller_id=controller_id,
            match_count=1,
            candidate_run_ids=[scheduler_candidate.run_id],
        )

    try:
        scheduler_matches = find_scheduler_candidates(
            controller_session_id=controller_id,
            repository_root=repo_root,
            include_terminal=include_terminal,
        )
    except SchedulerEngineError as exc:
        if exc.kind == SchedulerEngineErrorKind.CORRUPTION:
            return _read_failure_result(
                controller_id=controller_id,
                repo_root=repo_root,
                message="scheduler run snapshot is corrupt; inspect engine artifacts manually",
            )
        raise
    if not scheduler_matches:
        return _empty_controller_result(controller_id=controller_id, repo_root=repo_root)
    if len(scheduler_matches) > 1:
        candidate_ids = [candidate.run_id for candidate in scheduler_matches]
        return _ambiguous_controller_result(
            controller_id=controller_id,
            repo_root=repo_root,
            candidate_run_ids=candidate_ids,
            match_count=len(scheduler_matches),
        )
    return _build_scheduler_result(
        candidate=scheduler_matches[0],
        controller_id=controller_id,
        match_count=1,
        candidate_run_ids=[scheduler_matches[0].run_id],
    )


def _load_scheduler_candidate_or_failure(
    *,
    run_id: str,
    controller_id: str,
    repo_root: Path,
) -> tuple[ControllerSchedulerCandidate | None, str | None]:
    try:
        return (
            load_scheduler_candidate(
                run_id=run_id,
                controller_session_id=controller_id,
                repository_root=repo_root,
            ),
            None,
        )
    except SchedulerEngineError as exc:
        if exc.kind == SchedulerEngineErrorKind.CORRUPTION:
            return None, "scheduler run snapshot is corrupt; inspect engine artifacts manually"
        if exc.kind in {
            SchedulerEngineErrorKind.NOT_FOUND,
            SchedulerEngineErrorKind.VALIDATION,
        }:
            return None, None
        return None, str(exc)


def _read_failure_result(
    *,
    controller_id: str,
    repo_root: Path,
    message: str,
) -> ControllerStatusResult:
    return ControllerStatusResult(
        match_count=0,
        run_id=None,
        status=None,
        project=None,
        repository=str(repo_root),
        current_review_iteration=None,
        max_review_iterations=None,
        next_safe_action=message,
        last_error=None,
        result=None,
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id_prefix=None,
        candidate_run_ids=[],
        read_failure=message,
    )


def _empty_controller_result(
    *,
    controller_id: str,
    repo_root: Path,
) -> ControllerStatusResult:
    return ControllerStatusResult(
        match_count=0,
        run_id=None,
        status=None,
        project=None,
        repository=str(repo_root),
        current_review_iteration=None,
        max_review_iterations=None,
        next_safe_action=(
            "No matching non-terminal scheduler run for this controller session and repository. "
            "Confirm the exact controller session ID and repository path, or pass "
            "--run-id to disambiguate a known run."
        ),
        last_error=None,
        result=None,
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id_prefix=None,
        candidate_run_ids=[],
    )


def _ambiguous_controller_result(
    *,
    controller_id: str,
    repo_root: Path,
    candidate_run_ids: list[str],
    match_count: int | None = None,
) -> ControllerStatusResult:
    resolved_match_count = match_count if match_count is not None else len(candidate_run_ids)
    return ControllerStatusResult(
        match_count=resolved_match_count,
        run_id=None,
        status=None,
        project=None,
        repository=str(repo_root),
        current_review_iteration=None,
        max_review_iterations=None,
        next_safe_action=(
            f"Multiple matching scheduler runs ({resolved_match_count}). Pass --run-id with one of: "
            + ", ".join(candidate_run_ids)
            + ". Do not choose a run by timestamp."
        ),
        last_error=None,
        result=None,
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id_prefix=None,
        candidate_run_ids=candidate_run_ids,
    )


def _build_scheduler_result(
    *,
    candidate: ControllerSchedulerCandidate,
    controller_id: str,
    match_count: int,
    candidate_run_ids: list[str],
) -> ControllerStatusResult:
    capacity_prefix = None
    if candidate.capacity_holder_run_id is not None:
        run_id = candidate.capacity_holder_run_id
        capacity_prefix = run_id[:8] if len(run_id) > 8 else run_id
    next_action = candidate.safe_next_action.command or (
        "Inspect scheduler artifacts for the blocked run."
    )
    abort_control: dict[str, object] | None = None
    if candidate.state_kind in SCHEDULER_ABORTABLE_STATE_KINDS:
        abort_hint = scheduler_abort_safe_next_action(candidate.run_id)
        if candidate.safe_next_action.kind in {
            SafeNextActionKind.SCHEDULER_TICK,
            SafeNextActionKind.WAIT_UNTIL,
        }:
            next_action = f"{next_action} To stop safely: {abort_hint.command}"
        abort_control = {
            "abort_requested": False,
            "scheduler_abort_command": abort_hint.command,
            "active_process_registered": candidate.capacity_holder_run_id == candidate.run_id,
            "active_component": "scheduler_attempt"
            if candidate.capacity_holder_run_id == candidate.run_id
            else None,
            "active_iteration": None,
        }
    return ControllerStatusResult(
        match_count=match_count,
        run_id=candidate.run_id,
        status=candidate.state_kind,
        project=candidate.project_name,
        repository=candidate.repository_root,
        current_review_iteration=None,
        max_review_iterations=None,
        next_safe_action=next_action,
        last_error=None,
        result=None,
        abort_control=abort_control,
        controller_session_id=controller_id,
        reviewer_session_id_prefix=candidate.reviewer_session_id_prefix,
        candidate_run_ids=candidate_run_ids,
        scheduler_state_kind=candidate.state_kind,
        capacity_holder_run_id_prefix=capacity_prefix,
        last_event_kind=candidate.last_event_kind,
        cursor_wait_until=candidate.cursor_wait_until,
        block_reason_kind=candidate.block_reason_kind,
        run_source="scheduler",
    )


def render_controller_status(result: ControllerStatusResult, *, output: str = "text") -> str:
    if output == "json":
        payload = {
            "schema_version": 1,
            "match_count": result.match_count,
            "run_id": result.run_id,
            "status": result.status,
            "project": result.project,
            "repository": result.repository,
            "current_review_iteration": result.current_review_iteration,
            "max_review_iterations": result.max_review_iterations,
            "next_safe_action": result.next_safe_action,
            "last_error": result.last_error,
            "result": result.result,
            "abort_control": result.abort_control,
            "controller_session_id_prefix": result.controller_session_id[:8],
            "reviewer_session_id_prefix": result.reviewer_session_id_prefix,
            "candidate_run_ids": result.candidate_run_ids,
            "scheduler_state_kind": result.scheduler_state_kind,
            "capacity_holder_run_id_prefix": result.capacity_holder_run_id_prefix,
            "last_event_kind": result.last_event_kind,
            "cursor_wait_until": result.cursor_wait_until,
            "block_reason_kind": result.block_reason_kind,
            "run_source": result.run_source,
            "read_failure": result.read_failure,
        }
        return json.dumps(payload, indent=2) + "\n"

    lines = [
        "ai_dev_loop controller status",
        f"Controller: {result.controller_session_id[:8]}",
        f"Matches: {result.match_count}",
    ]
    if result.run_id is not None:
        lines.extend(
            [
                f"Run: {result.run_id}",
                f"Status: {result.status}",
                f"Project: {result.project}",
                f"Repository: {result.repository}",
            ]
        )
        if result.run_source == "scheduler":
            lines.append(f"Scheduler state: {result.scheduler_state_kind}")
            if result.last_event_kind:
                lines.append(f"Last event: {result.last_event_kind}")
            if result.cursor_wait_until:
                lines.append(f"Cursor wait until: {result.cursor_wait_until}")
            if result.block_reason_kind:
                lines.append(f"Block reason: {result.block_reason_kind}")
            if result.capacity_holder_run_id_prefix:
                lines.append(f"Capacity holder prefix: {result.capacity_holder_run_id_prefix}")
        if result.abort_control and result.abort_control.get("abort_requested"):
            lines.append("Abort request: pending")
        if result.abort_control and result.abort_control.get("active_process_registered"):
            lines.append(
                "Active child: "
                f"{result.abort_control.get('active_component')} "
                f"iteration {result.abort_control.get('active_iteration')}"
            )
        if result.last_error:
            lines.append(f"Last error: {result.last_error}")
        if result.result:
            lines.append(f"Result: {result.result}")
    elif result.read_failure:
        lines.append(f"Read failure: {result.read_failure}")
    elif result.candidate_run_ids:
        lines.append("Candidate runs: " + ", ".join(result.candidate_run_ids))
    lines.append(f"Next safe action: {result.next_safe_action}")
    return "\n".join(lines) + "\n"

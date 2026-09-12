"""Read-only controller status lookup by exact run or legacy controller identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.scheduler.application.contracts import (
    ControllerSchedulerCandidate,
    SafeNextActionKind,
    scheduler_abort_safe_next_action,
)
from ai_dev_loop.scheduler.application.controller_read import (
    find_scheduler_candidates,
    load_scheduler_candidate_by_run,
)
from ai_dev_loop.scheduler.domain.state import SCHEDULER_ABORTABLE_STATE_KINDS


@dataclass(frozen=True)
class ControllerStatusResult:
    match_count: int
    run_id: str | None
    status: str | None
    project: str | None
    repository: str | None
    review_iterations_completed: int | None
    max_review_iterations: int | None
    next_safe_action: str
    last_error: str | None
    result: str | None
    abort_control: dict[str, object] | None
    controller_session_id: str | None
    controller_session_id_prefix: str | None
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
    controller_session_id: str | None = None,
    repo_path: Path,
    run_id: str | None = None,
    include_terminal: bool = False,
) -> ControllerStatusResult:
    repo_info = discover_repository(repo_path)
    repo_root = repo_info.root.resolve()

    if run_id is not None:
        try:
            candidate = load_scheduler_candidate_by_run(
                run_id=run_id,
                repository_root=repo_root,
            )
        except Exception as exc:
            from ai_dev_loop.scheduler.application.contracts import (
                SchedulerEngineError,
                SchedulerEngineErrorKind,
            )

            if isinstance(exc, SchedulerEngineError):
                if exc.kind == SchedulerEngineErrorKind.CORRUPTION:
                    return _read_failure_result(
                        controller_id=controller_session_id,
                        repo_root=repo_root,
                        message="scheduler run snapshot is corrupt; inspect engine artifacts manually",
                    )
                if exc.kind in {
                    SchedulerEngineErrorKind.NOT_FOUND,
                    SchedulerEngineErrorKind.VALIDATION,
                }:
                    return _empty_controller_result(
                        controller_id=controller_session_id,
                        repo_root=repo_root,
                    )
            raise
        return _build_scheduler_result(
            candidate=candidate,
            controller_id=controller_session_id or candidate.controller_session_id_prefix,
            match_count=1,
            candidate_run_ids=[candidate.run_id],
        )

    if controller_session_id is None:
        raise ValidationError(
            "controller status requires --run-id or --controller-session-id; "
            "do not choose a run by repository recency"
        )

    controller_id = require_codex_session_id(controller_session_id)

    try:
        scheduler_matches = find_scheduler_candidates(
            controller_session_id=controller_id,
            repository_root=repo_root,
            include_terminal=include_terminal,
        )
    except Exception as exc:
        from ai_dev_loop.scheduler.application.contracts import (
            SchedulerEngineError,
            SchedulerEngineErrorKind,
        )

        if (
            isinstance(exc, SchedulerEngineError)
            and exc.kind == SchedulerEngineErrorKind.CORRUPTION
        ):
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


def _read_failure_result(
    *,
    controller_id: str | None,
    repo_root: Path,
    message: str,
) -> ControllerStatusResult:
    prefix = controller_id[:8] if controller_id else None
    return ControllerStatusResult(
        match_count=0,
        run_id=None,
        status=None,
        project=None,
        repository=str(repo_root),
        review_iterations_completed=None,
        max_review_iterations=None,
        next_safe_action=message,
        last_error=None,
        result=None,
        abort_control=None,
        controller_session_id=controller_id,
        controller_session_id_prefix=prefix,
        reviewer_session_id_prefix=None,
        candidate_run_ids=[],
        read_failure=message,
    )


def _empty_controller_result(
    *,
    controller_id: str | None,
    repo_root: Path,
) -> ControllerStatusResult:
    prefix = controller_id[:8] if controller_id else None
    return ControllerStatusResult(
        match_count=0,
        run_id=None,
        status=None,
        project=None,
        repository=str(repo_root),
        review_iterations_completed=None,
        max_review_iterations=None,
        next_safe_action=(
            "No matching non-terminal scheduler run for this repository selection. "
            "Confirm the exact run ID and repository path, or pass "
            "--controller-session-id for legacy discovery."
        ),
        last_error=None,
        result=None,
        abort_control=None,
        controller_session_id=controller_id,
        controller_session_id_prefix=prefix,
        reviewer_session_id_prefix=None,
        candidate_run_ids=[],
    )


def _ambiguous_controller_result(
    *,
    controller_id: str | None,
    repo_root: Path,
    candidate_run_ids: list[str],
    match_count: int | None = None,
) -> ControllerStatusResult:
    resolved_match_count = match_count if match_count is not None else len(candidate_run_ids)
    prefix = controller_id[:8] if controller_id else None
    return ControllerStatusResult(
        match_count=resolved_match_count,
        run_id=None,
        status=None,
        project=None,
        repository=str(repo_root),
        review_iterations_completed=None,
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
        controller_session_id_prefix=prefix,
        reviewer_session_id_prefix=None,
        candidate_run_ids=candidate_run_ids,
    )


def _build_scheduler_result(
    *,
    candidate: ControllerSchedulerCandidate,
    controller_id: str | None,
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
    controller_prefix = candidate.controller_session_id_prefix
    if controller_id is not None and controller_prefix is None:
        controller_prefix = controller_id[:8]
    return ControllerStatusResult(
        match_count=match_count,
        run_id=candidate.run_id,
        status=candidate.state_kind,
        project=candidate.project_name,
        repository=candidate.repository_root,
        review_iterations_completed=candidate.review_iterations_completed,
        max_review_iterations=candidate.max_review_iterations,
        next_safe_action=next_action,
        last_error=None,
        result=None,
        abort_control=abort_control,
        controller_session_id=controller_id,
        controller_session_id_prefix=controller_prefix,
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
            "review_iterations_completed": result.review_iterations_completed,
            "max_review_iterations": result.max_review_iterations,
            "next_safe_action": result.next_safe_action,
            "last_error": result.last_error,
            "result": result.result,
            "abort_control": result.abort_control,
            "controller_session_id_prefix": result.controller_session_id_prefix,
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
        f"Matches: {result.match_count}",
    ]
    if result.controller_session_id_prefix:
        lines.insert(1, f"Controller: {result.controller_session_id_prefix}")
    if result.run_id is not None:
        lines.extend(
            [
                f"Run: {result.run_id}",
                f"Status: {result.status}",
                f"Project: {result.project}",
                f"Repository: {result.repository}",
            ]
        )
        if result.review_iterations_completed is not None:
            lines.append(
                "Reviews completed: "
                f"{result.review_iterations_completed}/{result.max_review_iterations}"
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

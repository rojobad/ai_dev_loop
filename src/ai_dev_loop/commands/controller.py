"""Read-only controller status lookup by exact controller session identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.abort_control import abort_control_summary
from ai_dev_loop.commands.status import render_status
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.launcher import launcher_summary
from ai_dev_loop.resume_planner import TERMINAL_STATUSES
from ai_dev_loop.run_discovery import find_runs_for_controller, load_run
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.scheduler.application.contracts import (
    ControllerSchedulerCandidate,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.controller_read import (
    find_scheduler_candidates,
    load_scheduler_candidate,
)
from ai_dev_loop.state import RunState, RunStatus, shorten_session_id


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
    launcher: dict[str, object]
    abort_control: dict[str, object] | None
    controller_session_id: str
    reviewer_session_id: str | None
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
        legacy_match: tuple[Path, RunState] | None = None
        try:
            run_directory, state = load_run(run_id)
            _require_controller_match(state, controller_id=controller_id, repo_root=repo_root)
            legacy_match = (run_directory, state)
        except ValidationError:
            legacy_match = None
        source_count = int(scheduler_candidate is not None) + int(legacy_match is not None)
        if source_count == 0:
            return _empty_controller_result(controller_id=controller_id, repo_root=repo_root)
        if source_count > 1:
            return _ambiguous_controller_result(
                controller_id=controller_id,
                repo_root=repo_root,
                candidate_run_ids=[run_id],
                match_count=source_count,
            )
        if scheduler_candidate is not None:
            return _build_scheduler_result(
                candidate=scheduler_candidate,
                controller_id=controller_id,
                match_count=1,
                candidate_run_ids=[scheduler_candidate.run_id],
            )
        assert legacy_match is not None
        run_directory, state = legacy_match
        return _build_result(
            state=state,
            run_directory=run_directory,
            controller_id=controller_id,
            match_count=1,
            candidate_run_ids=[state.run_id],
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
    legacy_matches = find_runs_for_controller(
        controller_session_id=controller_id,
        repository_root=repo_root,
        include_terminal=include_terminal,
    )
    combined_count = len(scheduler_matches) + len(legacy_matches)
    if combined_count == 0:
        return _empty_controller_result(controller_id=controller_id, repo_root=repo_root)
    if combined_count > 1 or (scheduler_matches and legacy_matches):
        candidate_ids = [candidate.run_id for candidate in scheduler_matches] + [
            state.run_id for _, state in legacy_matches
        ]
        return _ambiguous_controller_result(
            controller_id=controller_id,
            repo_root=repo_root,
            candidate_run_ids=candidate_ids,
            match_count=combined_count,
        )
    if scheduler_matches:
        return _build_scheduler_result(
            candidate=scheduler_matches[0],
            controller_id=controller_id,
            match_count=1,
            candidate_run_ids=[scheduler_matches[0].run_id],
        )
    run_directory, state = legacy_matches[0]
    return _build_result(
        state=state,
        run_directory=run_directory,
        controller_id=controller_id,
        match_count=1,
        candidate_run_ids=[state.run_id],
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
        launcher={
            "launcher_registered": False,
            "launcher_live": False,
            "launcher_stale": False,
            "launcher_outcome": None,
        },
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id=None,
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
            "No matching non-terminal run for this controller session and repository. "
            "Confirm the exact controller session ID and repository path, or pass "
            "--run-id to disambiguate a known run."
        ),
        last_error=None,
        result=None,
        launcher={
            "launcher_registered": False,
            "launcher_live": False,
            "launcher_stale": False,
            "launcher_outcome": None,
        },
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id=None,
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
            f"Multiple matching runs ({resolved_match_count}). Pass --run-id with one of: "
            + ", ".join(candidate_run_ids)
            + ". Do not choose a run by timestamp."
        ),
        last_error=None,
        result=None,
        launcher={
            "launcher_registered": False,
            "launcher_live": False,
            "launcher_stale": False,
            "launcher_outcome": None,
        },
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id=None,
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
    return ControllerStatusResult(
        match_count=match_count,
        run_id=candidate.run_id,
        status=candidate.state_kind,
        project=candidate.project_name,
        repository=candidate.repository_root,
        current_review_iteration=None,
        max_review_iterations=None,
        next_safe_action=candidate.safe_next_action.command
        or "Inspect scheduler artifacts for the blocked run.",
        last_error=None,
        result=None,
        launcher={
            "launcher_registered": False,
            "launcher_live": False,
            "launcher_stale": False,
            "launcher_outcome": None,
        },
        abort_control=None,
        controller_session_id=controller_id,
        reviewer_session_id=None,
        candidate_run_ids=candidate_run_ids,
        scheduler_state_kind=candidate.state_kind,
        capacity_holder_run_id_prefix=capacity_prefix,
        last_event_kind=candidate.last_event_kind,
        cursor_wait_until=candidate.cursor_wait_until,
        block_reason_kind=candidate.block_reason_kind,
        run_source="scheduler",
    )


def _require_controller_match(
    state: RunState,
    *,
    controller_id: str,
    repo_root: Path,
) -> None:
    if state.controller is None:
        raise ValidationError(
            "run has no controller metadata; controller status requires an A/B-prepared run"
        )
    if state.controller.controller_session_id != controller_id:
        raise ValidationError("controller session id does not match the selected run")
    if Path(state.repository.root).resolve() != repo_root:
        raise ValidationError("repository path does not match the selected run repository root")


def _build_result(
    *,
    state: RunState,
    run_directory: Path,
    controller_id: str,
    match_count: int,
    candidate_run_ids: list[str],
) -> ControllerStatusResult:
    # Reuse status next-action text without printing sensitive fields.
    status_text = render_status(state.run_id, output="json")
    status_payload = json.loads(status_text)
    next_action = str(status_payload["next_safe_action"])
    if (
        state.status in {RunStatus.PREPARED, RunStatus.WAITING_FOR_CURSOR_FIX}
        and state.controller is not None
    ):
        if state.codex.fresh_reviewer is not None:
            next_action = (
                "Launch from the controller session with "
                f"ai_dev_loop launch {state.run_id} --controller-session-id <exact-controller-id>."
            )
        else:
            next_action = (
                "This run uses the retired session-bound A/B contract. Inspect only, "
                "then prepare a fresh run."
            )
    launcher = launcher_summary(run_directory, run_id=state.run_id)
    control = abort_control_summary(run_directory)
    if launcher.get("launcher_live"):
        next_action = (
            "Detached worker is live. Wait, poll controller status again, or "
            f"abort with ai_dev_loop abort {state.run_id}."
        )
    elif state.status in TERMINAL_STATUSES:
        next_action = str(status_payload["next_safe_action"])
    return ControllerStatusResult(
        match_count=match_count,
        run_id=state.run_id,
        status=state.status.value,
        project=state.project.name,
        repository=state.repository.root,
        current_review_iteration=state.workflow.current_review_iteration,
        max_review_iterations=state.workflow.max_review_iterations,
        next_safe_action=next_action,
        last_error=state.last_error,
        result=state.result,
        launcher=launcher,
        abort_control=control,
        controller_session_id=controller_id,
        reviewer_session_id=state.codex.session_id,
        candidate_run_ids=candidate_run_ids,
        run_source="legacy",
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
            "launcher": result.launcher,
            "abort_control": result.abort_control,
            "controller_session_id_prefix": result.controller_session_id[:8],
            "reviewer_session_id_prefix": None
            if result.reviewer_session_id is None
            else result.reviewer_session_id[:8],
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
        f"Controller: {shorten_session_id(result.controller_session_id)}",
        f"Matches: {result.match_count}",
    ]
    if result.run_id is not None:
        lines.extend(
            [
                f"Run: {result.run_id}",
                f"Status: {result.status}",
                f"Project: {result.project}",
                f"Repository: {result.repository}",
                (
                    f"Review iteration: {result.current_review_iteration}/"
                    f"{result.max_review_iterations}"
                ),
            ]
        )
        if result.reviewer_session_id:
            lines.append(f"Reviewer: {shorten_session_id(result.reviewer_session_id)}")
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
        launcher = result.launcher
        lines.append(f"Worker live: {launcher.get('launcher_live')}")
        if launcher.get("launcher_stale"):
            lines.append(f"Worker stale: {launcher.get('stale_reason')}")
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

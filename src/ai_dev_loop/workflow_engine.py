"""Shared bounded loop workflow for start and resume."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.commands.start_preflight import (
    begin_reviewing,
    begin_running_cursor,
    begin_staging,
    begin_validating,
    mark_completed,
    mark_completed_with_residual_risk,
    mark_failed,
    mark_interrupted,
    mark_max_iterations_reached,
    mark_waiting_for_cursor_fix,
    run_resume_preflight_checks,
    run_start_preflight_checks,
    validate_start_status,
)
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.event_log import EventLevel, append_orchestrator_event
from ai_dev_loop.iterations import (
    cursor_prompt_path,
    iteration_label,
    max_iteration_number,
    read_cursor_prompt,
    upsert_iteration,
)
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.resume_planner import (
    TERMINAL_STATUSES,
    WorkflowActionKind,
    cursor_turn_complete,
    has_workflow_progress,
    plan_next_action,
    preserve_artifact_before_retry,
    requires_persisted_cursor_chat_id,
    restore_interrupted_checkpoint,
    restore_workflow_checkpoint,
    validate_recorded_staged_patch_for_review,
)
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.codex import (
    load_review_result_from_artifacts,
    result_message_for_loop_continue,
    result_message_for_max_iterations,
    result_message_for_review,
    run_codex_review,
)
from ai_dev_loop.runners.cursor import create_chat, execute_prompt
from ai_dev_loop.runners.git import validate_correction_pre_cursor
from ai_dev_loop.runners.probes import (
    capture_git_status,
    require_probe_success,
    run_start_probes,
)
from ai_dev_loop.runners.staging import run_git_staging
from ai_dev_loop.state import (
    RunState,
    RunStatus,
    append_run_log,
    atomic_write_json,
    atomic_write_text,
    save_run_state,
)


@dataclass(frozen=True)
class WorkflowResult:
    run_id: str
    status: str
    chat_id: str
    iteration_count: int
    latest_staged_diff_path: str | None
    latest_review_path: str | None
    result_message: str

    @property
    def staged_diff_path(self) -> str | None:
        return self.latest_staged_diff_path

    @property
    def iteration_dir(self) -> str:
        if self.iteration_count <= 0:
            return "cursor/iterations/01"
        return f"cursor/iterations/{self.iteration_count:02d}"


def _fail_run(
    run_directory: Path,
    state: RunState,
    message: str,
    *,
    event_name: str = "workflow_failed",
) -> None:
    mark_failed(state, message)
    save_run_state(run_directory, state)
    append_run_log(run_directory, message)
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event=event_name,
        level=EventLevel.ERROR,
        status=state.status.value,
        detail={"message": message},
    )


def start_run(run_id: str) -> WorkflowResult:
    run_directory, state = load_run(run_id)
    metadata = _lock_metadata(run_id, state)
    with RunLocks(run_directory, metadata):
        _log_requested(run_directory, run_id, state, event="start_requested")
        try:
            validate_start_status(state)
        except ValidationError as exc:
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

        begin_validating(state)
        save_run_state(run_directory, state)
        _run_preflight(run_directory, state, from_prepared=True)
        _run_probes(run_directory, state)
        return _continue_workflow(run_directory, state)


def resume_run(run_id: str) -> WorkflowResult:
    from ai_dev_loop.commands.start_preflight import validate_resume_status

    run_directory, state = load_run(run_id)
    metadata = _lock_metadata(run_id, state)
    with RunLocks(run_directory, metadata):
        _log_requested(run_directory, run_id, state, event="resume_requested")
        try:
            validate_resume_status(state)
        except ValidationError as exc:
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

        from_prepared = state.status == RunStatus.PREPARED
        if state.status == RunStatus.INTERRUPTED:
            restore_interrupted_checkpoint(state, run_directory)
            save_run_state(run_directory, state)
        if from_prepared:
            begin_validating(state)
            save_run_state(run_directory, state)
            _run_preflight(run_directory, state, from_prepared=True)
            _run_probes(run_directory, state)
        else:
            run_resume_preflight_checks(state, run_directory, from_prepared=False)

        if state.status == RunStatus.WAITING_FOR_CURSOR_FIX:
            if state.workflow.current_review_iteration >= state.workflow.max_review_iterations:
                mark_max_iterations_reached(state, result_message_for_max_iterations())
                save_run_state(run_directory, state)
                chat_id = _require_cursor_chat_or_fail(run_directory, state)
                return WorkflowResult(
                    run_id=state.run_id,
                    status=state.status.value,
                    chat_id=chat_id,
                    iteration_count=state.workflow.current_review_iteration,
                    latest_staged_diff_path=_latest_staged_diff(state),
                    latest_review_path=_latest_review_path(state),
                    result_message=state.result or result_message_for_max_iterations(),
                )
            begin_running_cursor(state)
            save_run_state(run_directory, state)

        return _continue_workflow(run_directory, state)


def _lock_metadata(run_id: str, state: RunState) -> LockMetadata:
    return LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=state.repository.root,
        started_at=datetime.now(tz=UTC),
    )


def _log_requested(run_directory: Path, run_id: str, state: RunState, *, event: str) -> None:
    append_run_log(run_directory, f"{event.replace('_', ' ')} for run {run_id}")
    append_orchestrator_event(
        run_directory,
        run_id=run_id,
        component="orchestrator",
        event=event,
        status=state.status.value,
    )


def _run_preflight(run_directory: Path, state: RunState, *, from_prepared: bool) -> None:
    try:
        if from_prepared:
            run_start_preflight_checks(state, run_directory)
        else:
            run_resume_preflight_checks(state, run_directory, from_prepared=False)
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="preflight_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
    append_run_log(run_directory, "preflight passed")
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="preflight_passed",
        status=state.status.value,
    )


def _run_probes(run_directory: Path, state: RunState) -> None:
    try:
        probe_results = run_start_probes(
            cursor_command=state.cursor.command,
            cursor_model=state.cursor.model,
            codex_command=state.codex.command,
        )
        require_probe_success(probe_results)
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="probe_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
    append_run_log(run_directory, "local CLI probes passed")
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="probes_passed",
        status=state.status.value,
    )


def _require_cursor_chat(run_directory: Path, state: RunState) -> str:
    if state.cursor.chat_id:
        return state.cursor.chat_id
    if requires_persisted_cursor_chat_id(state, run_directory):
        raise ValidationError(
            "cursor chat id is missing from checkpointed run state; cannot create a new chat"
        )
    chat_id = create_chat(state.cursor.command)
    state.cursor.chat_id = chat_id
    save_run_state(run_directory, state)
    chat_payload = {
        "chat_id": chat_id,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "command": state.cursor.command,
    }
    chat_path = run_directory / "cursor" / "chat.json"
    atomic_write_json(chat_path, chat_payload, sensitive=True)
    set_sensitive_file_mode(chat_path)
    append_run_log(run_directory, f"created Cursor chat {chat_id}")
    return chat_id


def _require_cursor_chat_or_fail(run_directory: Path, state: RunState) -> str:
    try:
        return _require_cursor_chat(run_directory, state)
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="cursor_chat_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc


def _continue_workflow(run_directory: Path, state: RunState) -> WorkflowResult:
    if state.status == RunStatus.VALIDATING and has_workflow_progress(state, run_directory):
        restore_workflow_checkpoint(state, run_directory)
        save_run_state(run_directory, state)

    chat_id = _require_cursor_chat_or_fail(run_directory, state)
    latest_staged_diff: str | None = None
    latest_review_path: str | None = None
    result_message = ""

    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="loop_started",
        status=state.status.value,
        detail={"max_review_iterations": state.workflow.max_review_iterations},
    )

    while state.status not in TERMINAL_STATUSES:
        action = plan_next_action(state, run_directory)
        if action is None:
            break

        if action.kind == WorkflowActionKind.CURSOR:
            result_message = _run_cursor_turn(
                run_directory,
                state,
                chat_id=chat_id,
                iteration_number=action.iteration_number,
            )
        elif action.kind == WorkflowActionKind.STAGING:
            latest_staged_diff = _run_staging_pass(
                run_directory,
                state,
                iteration_number=action.iteration_number,
            )
        elif action.kind == WorkflowActionKind.REVIEW:
            latest_review_path, result_message = _run_review_pass(
                run_directory,
                state,
                iteration_number=action.iteration_number,
            )
        elif action.kind == WorkflowActionKind.PROCESS_REVIEW:
            latest_review_path, result_message, should_continue = _process_review_outcome(
                run_directory,
                state,
                iteration_number=action.iteration_number,
            )
            if not should_continue:
                break

    return WorkflowResult(
        run_id=state.run_id,
        status=state.status.value,
        chat_id=chat_id,
        iteration_count=max(max_iteration_number(state), state.workflow.current_review_iteration),
        latest_staged_diff_path=latest_staged_diff or _latest_staged_diff(state),
        latest_review_path=latest_review_path or _latest_review_path(state),
        result_message=result_message or (state.result or ""),
    )


def _latest_staged_diff(state: RunState) -> str | None:
    if not state.iterations:
        return None
    latest = max(state.iterations, key=lambda entry: entry.get("number", 0))
    git_section = latest.get("git")
    if isinstance(git_section, dict):
        path = git_section.get("staged_diff_path")
        return path if isinstance(path, str) else None
    return None


def _latest_review_path(state: RunState) -> str | None:
    if not state.iterations:
        return None
    latest = max(state.iterations, key=lambda entry: entry.get("number", 0))
    codex_section = latest.get("codex")
    if isinstance(codex_section, dict):
        path = codex_section.get("result_path")
        return path if isinstance(path, str) else None
    return None


def _run_cursor_turn(
    run_directory: Path,
    state: RunState,
    *,
    chat_id: str,
    iteration_number: int,
) -> str:
    iteration = iteration_label(iteration_number)
    iteration_dir = run_directory / "cursor" / "iterations" / iteration
    iteration_dir.mkdir(parents=True, exist_ok=True)

    if iteration_number > 1:
        from ai_dev_loop.iterations import previous_iteration_git_patch_path

        try:
            patch_rel = previous_iteration_git_patch_path(state, iteration_number)
            if patch_rel is None:
                raise ValidationError("previous staged patch metadata is missing for correction")
            validate_correction_pre_cursor(
                Path(state.repository.root),
                patch_artifact=run_directory / patch_rel,
            )
        except ValidationError as exc:
            _fail_run(run_directory, state, str(exc), event_name="correction_preflight_failed")
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

    if state.status != RunStatus.RUNNING_CURSOR:
        begin_running_cursor(state)
        save_run_state(run_directory, state)
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="cursor",
        event="execution_started",
        status=state.status.value,
        iteration=iteration_number,
    )

    cursor_started_at = datetime.now(tz=UTC)
    try:
        prompt_rel = cursor_prompt_path(state, iteration_number)
        prompt = read_cursor_prompt(state, run_directory, iteration_number)
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="cursor_prompt_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

    upsert_iteration(
        state,
        {
            "number": iteration_number,
            "kind": "initial_implementation" if iteration_number == 1 else "cursor_correction",
            "started_at": cursor_started_at.astimezone(UTC).isoformat(),
            "cursor": {"prompt_path": prompt_rel},
        },
    )
    save_run_state(run_directory, state)

    before_status_path = run_directory / "git" / "status" / f"{iteration}-before-cursor.txt"
    after_status_path = run_directory / "git" / "status" / f"{iteration}-after-cursor.txt"
    events_path = iteration_dir / "events.jsonl"
    stderr_path = iteration_dir / "stderr.txt"

    if not cursor_turn_complete(run_directory, iteration_number):
        preserve_artifact_before_retry(events_path)
        preserve_artifact_before_retry(stderr_path)
        preserve_artifact_before_retry(iteration_dir / "metadata.json")
        preserve_artifact_before_retry(iteration_dir / "final.txt")

    try:
        atomic_write_text(before_status_path, capture_git_status(state.repository.root) + "\n")
        timeout_seconds = state.workflow.cursor_timeout_minutes * 60
        execution = execute_prompt(
            state.cursor,
            repo_root=state.repository.root,
            chat_id=chat_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            stdout_path=events_path,
            stderr_path=stderr_path,
        )
        atomic_write_text(after_status_path, capture_git_status(state.repository.root) + "\n")

        metadata_payload = {
            "args": execution.metadata_args,
            "exit_code": execution.process.returncode,
            "elapsed_seconds": execution.process.elapsed_seconds,
            "timed_out": execution.process.timed_out,
            "parse_ok": execution.parse.parse_ok,
            "errors": list(execution.parse.errors),
        }
        atomic_write_json(iteration_dir / "metadata.json", metadata_payload, sensitive=True)
        if execution.parse.final_text:
            atomic_write_text(
                iteration_dir / "final.txt", execution.parse.final_text, sensitive=True
            )
    except AiDevLoopError as exc:
        _fail_run(run_directory, state, str(exc), event_name="cursor_execution_failed")
        raise
    except OSError as exc:
        message = f"Cursor artifact capture failed: {exc}"
        _fail_run(run_directory, state, message, event_name="cursor_artifact_failed")
        raise AiDevLoopError(message) from exc

    if execution.process.timed_out:
        mark_interrupted(state, "Cursor execution timed out")
        save_run_state(run_directory, state)
        append_orchestrator_event(
            run_directory,
            run_id=state.run_id,
            component="cursor",
            event="execution_timed_out",
            level=EventLevel.ERROR,
            status=state.status.value,
            iteration=iteration_number,
        )
        raise AiDevLoopError("Cursor execution timed out")

    if execution.process.returncode != 0:
        detail = execution.process.stderr.strip() or "Cursor execution failed"
        mark_failed(state, detail)
        save_run_state(run_directory, state)
        append_orchestrator_event(
            run_directory,
            run_id=state.run_id,
            component="cursor",
            event="execution_failed",
            level=EventLevel.ERROR,
            status=state.status.value,
            iteration=iteration_number,
            detail={"exit_code": execution.process.returncode},
        )
        raise AiDevLoopError(f"Cursor execution failed: {detail}")

    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="cursor",
        event="execution_complete",
        status=state.status.value,
        iteration=iteration_number,
        artifact_path=f"cursor/iterations/{iteration}/events.jsonl",
        detail={"exit_code": execution.process.returncode},
    )
    begin_staging(state)
    save_run_state(run_directory, state)
    return ""


def _run_staging_pass(
    run_directory: Path,
    state: RunState,
    *,
    iteration_number: int,
) -> str:
    iteration = iteration_label(iteration_number)
    if state.status == RunStatus.RUNNING_CURSOR:
        begin_staging(state)
        save_run_state(run_directory, state)
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="staging_started",
        status=state.status.value,
        iteration=iteration_number,
    )

    entry = next(
        (item for item in state.iterations if item.get("number") == iteration_number),
        None,
    )
    cursor_started_at = datetime.now(tz=UTC)
    cursor_exit_code = 0
    prompt_path = cursor_prompt_path(state, iteration_number)
    if entry:
        if isinstance(entry.get("started_at"), str):
            cursor_started_at = datetime.fromisoformat(entry["started_at"])
        cursor_section = entry.get("cursor")
        if isinstance(cursor_section, dict):
            if isinstance(cursor_section.get("exit_code"), int):
                cursor_exit_code = cursor_section["exit_code"]
            if isinstance(cursor_section.get("prompt_path"), str):
                prompt_path = cursor_section["prompt_path"]

    try:
        staging = run_git_staging(
            state,
            run_directory,
            iteration=iteration,
            iteration_number=iteration_number,
            cursor_started_at=cursor_started_at,
            cursor_exit_code=cursor_exit_code,
            prompt_path=prompt_path,
        )
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="staging_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
    except AiDevLoopError as exc:
        _fail_run(run_directory, state, str(exc), event_name="staging_failed")
        raise
    except OSError as exc:
        message = f"Git staging artifact capture failed: {exc}"
        _fail_run(run_directory, state, message, event_name="staging_artifact_failed")
        raise AiDevLoopError(message) from exc

    begin_reviewing(state)
    state.workflow.current_review_iteration = iteration_number
    save_run_state(run_directory, state)
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="git",
        event="staging_complete",
        status=state.status.value,
        iteration=iteration_number,
        artifact_path=staging.artifacts.patch_path,
    )
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="review_started",
        status=state.status.value,
        iteration=iteration_number,
    )
    return staging.artifacts.patch_path


def _ensure_reviewing_status(state: RunState, iteration_number: int) -> None:
    if state.status == RunStatus.STAGING:
        begin_reviewing(state)
        state.workflow.current_review_iteration = iteration_number


def _run_review_pass(
    run_directory: Path,
    state: RunState,
    *,
    iteration_number: int,
) -> tuple[str | None, str]:
    iteration = iteration_label(iteration_number)
    _ensure_reviewing_status(state, iteration_number)
    save_run_state(run_directory, state)
    try:
        validate_recorded_staged_patch_for_review(state, run_directory, iteration_number)
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="review_preflight_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
    events_path = run_directory / f"codex/events/{iteration}.jsonl"
    preserve_artifact_before_retry(events_path)
    preserve_artifact_before_retry(run_directory / f"codex/events/{iteration}.stderr.txt")
    preserve_artifact_before_retry(run_directory / f"codex/reviews/{iteration}.json")

    try:
        review_execution = run_codex_review(state, run_directory, iteration=iteration)
    except AiDevLoopError as exc:
        if "timed out" in str(exc).lower():
            mark_interrupted(state, str(exc))
            save_run_state(run_directory, state)
            append_orchestrator_event(
                run_directory,
                run_id=state.run_id,
                component="codex",
                event="review_timed_out",
                level=EventLevel.ERROR,
                status=state.status.value,
                iteration=iteration_number,
            )
            raise
        _fail_run(run_directory, state, str(exc), event_name="codex_review_failed")
        raise
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="codex_review_invalid")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
    except OSError as exc:
        message = f"Codex review artifact capture failed: {exc}"
        _fail_run(run_directory, state, message, event_name="codex_artifact_failed")
        raise AiDevLoopError(message) from exc

    _, result_message, _ = _apply_review_result(
        run_directory,
        state,
        iteration_number=iteration_number,
        review=review_execution.result,
        review_artifact_path=review_execution.artifacts.result_path,
    )
    return review_execution.artifacts.result_path, result_message


def _process_review_outcome(
    run_directory: Path,
    state: RunState,
    *,
    iteration_number: int,
) -> tuple[str | None, str, bool]:
    iteration = iteration_label(iteration_number)
    _ensure_reviewing_status(state, iteration_number)
    save_run_state(run_directory, state)
    try:
        validate_recorded_staged_patch_for_review(state, run_directory, iteration_number)
    except ValidationError as exc:
        _fail_run(run_directory, state, str(exc), event_name="review_preflight_failed")
        raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
    review = load_review_result_from_artifacts(run_directory, iteration)
    result_path = f"codex/reviews/{iteration}.json"
    return _apply_review_result(
        run_directory,
        state,
        iteration_number=iteration_number,
        review=review,
        review_artifact_path=result_path,
    )


def _apply_review_result(
    run_directory: Path,
    state: RunState,
    *,
    iteration_number: int,
    review: CodexReviewResult,
    review_artifact_path: str,
) -> tuple[str | None, str, bool]:
    result_message = result_message_for_review(review)
    should_continue = False

    if review.has_actionable_findings:
        if iteration_number >= state.workflow.max_review_iterations:
            mark_max_iterations_reached(state, result_message_for_max_iterations())
            result_message = result_message_for_max_iterations()
            append_orchestrator_event(
                run_directory,
                run_id=state.run_id,
                component="orchestrator",
                event="max_iterations_reached",
                status=state.status.value,
                iteration=iteration_number,
            )
        else:
            mark_waiting_for_cursor_fix(state, result_message_for_loop_continue())
            save_run_state(run_directory, state)
            append_orchestrator_event(
                run_directory,
                run_id=state.run_id,
                component="codex",
                event="fix_prompt_available",
                status=state.status.value,
                iteration=iteration_number,
            )
            begin_running_cursor(state)
            result_message = result_message_for_loop_continue()
            should_continue = True
    elif review.tests_status in {"failed", "blocked_environment", "skipped_findings_present"}:
        mark_completed_with_residual_risk(state, result_message)
    else:
        mark_completed(state, result_message)

    save_run_state(run_directory, state)
    append_run_log(run_directory, f"Codex review complete; status={state.status.value}")
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="codex",
        event="review_complete",
        status=state.status.value,
        iteration=iteration_number,
        artifact_path=review_artifact_path,
        detail={
            "has_actionable_findings": review.has_actionable_findings,
            "findings_count": review.findings_count,
            "tests_status": review.tests_status,
            "summary": review.summary,
        },
    )
    return review_artifact_path, result_message, should_continue


def render_workflow_output(result: WorkflowResult) -> str:
    lines = [
        f"Run {result.run_id}",
        f"Status: {result.status}",
        f"Cursor chat: {result.chat_id}",
        f"Iterations: {result.iteration_count}",
    ]
    if result.latest_staged_diff_path:
        lines.append(f"Staged diff: {result.latest_staged_diff_path}")
    if result.latest_review_path:
        lines.append(f"Latest review: {result.latest_review_path}")
    if result.result_message:
        lines.append(result.result_message)
    return "\n".join(lines) + "\n"

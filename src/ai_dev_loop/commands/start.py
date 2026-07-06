"""Start command: preflight, locks, Cursor chat creation, and initial execution."""

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
    mark_waiting_for_cursor_fix,
    run_start_preflight_checks,
    validate_start_status,
)
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.event_log import EventLevel, append_orchestrator_event
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.codex import (
    result_message_for_review,
    run_codex_review,
)
from ai_dev_loop.runners.cursor import create_chat, execute_prompt
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

CODEX_TUI_WARNING = "Important: exit the active Codex TUI before continuing with start."


@dataclass(frozen=True)
class StartResult:
    run_id: str
    status: str
    chat_id: str
    iteration_dir: str
    staged_diff_path: str
    result_message: str


def _fail_run(
    run_directory: Path,
    state: RunState,
    message: str,
    *,
    event_name: str = "start_failed",
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


def start_run(run_id: str) -> StartResult:
    run_directory, state = load_run(run_id)

    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=state.repository.root,
        started_at=datetime.now(tz=UTC),
    )

    with RunLocks(run_directory, metadata):
        append_run_log(run_directory, f"start requested for run {run_id}")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="start_requested",
            status=state.status.value,
        )

        try:
            validate_start_status(state)
        except ValidationError as exc:
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

        begin_validating(state)
        save_run_state(run_directory, state)
        append_run_log(run_directory, "status=validating")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="validating",
            status=state.status.value,
        )

        try:
            run_start_preflight_checks(state, run_directory)
        except ValidationError as exc:
            _fail_run(run_directory, state, str(exc), event_name="preflight_failed")
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
        append_run_log(run_directory, "preflight passed")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="preflight_passed",
            status=state.status.value,
        )

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
            run_id=run_id,
            component="orchestrator",
            event="probes_passed",
            status=state.status.value,
        )

        try:
            chat_id = _ensure_cursor_chat(run_directory, state)
        except ValidationError as exc:
            _fail_run(run_directory, state, str(exc), event_name="cursor_chat_failed")
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

        begin_running_cursor(state)
        save_run_state(run_directory, state)
        append_run_log(run_directory, f"Cursor chat ready: {chat_id}; status=running_cursor")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="cursor",
            event="chat_ready",
            status=state.status.value,
            detail={"chat_id": chat_id},
        )

        iteration = "01"
        iteration_dir = run_directory / "cursor" / "iterations" / iteration
        iteration_dir.mkdir(parents=True, exist_ok=True)
        cursor_started_at = datetime.now(tz=UTC)

        before_status_path = run_directory / "git" / "status" / f"{iteration}-before-cursor.txt"
        after_status_path = run_directory / "git" / "status" / f"{iteration}-after-cursor.txt"

        try:
            atomic_write_text(before_status_path, capture_git_status(state.repository.root) + "\n")

            prompt = (run_directory / state.prompt.snapshot_path).read_text(encoding="utf-8")
            timeout_seconds = state.workflow.cursor_timeout_minutes * 60
            execution = execute_prompt(
                state.cursor,
                repo_root=state.repository.root,
                chat_id=chat_id,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                stdout_path=iteration_dir / "events.jsonl",
                stderr_path=iteration_dir / "stderr.txt",
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
            append_run_log(run_directory, "Cursor execution timed out")
            append_orchestrator_event(
                run_directory,
                run_id=run_id,
                component="cursor",
                event="execution_timed_out",
                level=EventLevel.ERROR,
                status=state.status.value,
                iteration=1,
            )
            raise AiDevLoopError("Cursor execution timed out")

        if execution.process.returncode != 0:
            detail = execution.process.stderr.strip() or "Cursor execution failed"
            mark_failed(state, detail)
            save_run_state(run_directory, state)
            append_run_log(
                run_directory,
                f"Cursor execution failed: exit {execution.process.returncode}",
            )
            append_orchestrator_event(
                run_directory,
                run_id=run_id,
                component="cursor",
                event="execution_failed",
                level=EventLevel.ERROR,
                status=state.status.value,
                iteration=1,
                detail={"exit_code": execution.process.returncode},
            )
            raise AiDevLoopError(f"Cursor execution failed: {detail}")

        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="cursor",
            event="execution_complete",
            status=state.status.value,
            iteration=1,
            artifact_path=f"cursor/iterations/{iteration}/events.jsonl",
            detail={"exit_code": execution.process.returncode},
        )

        begin_staging(state)
        save_run_state(run_directory, state)
        append_run_log(run_directory, "Cursor execution complete; status=staging")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="staging_started",
            status=state.status.value,
            iteration=1,
        )

        try:
            staging = run_git_staging(
                state,
                run_directory,
                iteration=iteration,
                cursor_started_at=cursor_started_at,
                cursor_exit_code=execution.process.returncode,
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

        append_run_log(run_directory, "Git staging complete; status=reviewing")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="git",
            event="staging_complete",
            status=state.status.value,
            iteration=1,
            artifact_path=staging.artifacts.patch_path,
        )

        begin_reviewing(state)
        state.workflow.current_review_iteration = 1
        save_run_state(run_directory, state)
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="review_started",
            status=state.status.value,
            iteration=1,
        )

        try:
            review_execution = run_codex_review(state, run_directory, iteration=iteration)
        except AiDevLoopError as exc:
            if "timed out" in str(exc).lower():
                mark_interrupted(state, str(exc))
                save_run_state(run_directory, state)
                append_run_log(run_directory, str(exc))
                append_orchestrator_event(
                    run_directory,
                    run_id=run_id,
                    component="codex",
                    event="review_timed_out",
                    level=EventLevel.ERROR,
                    status=state.status.value,
                    iteration=1,
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

        result_message = result_message_for_review(review_execution.result)
        completion_status = review_execution.completion_status
        if completion_status == RunStatus.COMPLETED.value:
            mark_completed(state, result_message)
        elif completion_status == RunStatus.COMPLETED_WITH_RESIDUAL_RISK.value:
            mark_completed_with_residual_risk(state, result_message)
        else:
            mark_waiting_for_cursor_fix(state, result_message)

        save_run_state(run_directory, state)
        append_run_log(run_directory, f"Codex review complete; status={state.status.value}")
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="codex",
            event="review_complete",
            status=state.status.value,
            iteration=1,
            artifact_path=review_execution.artifacts.result_path,
            detail={
                "has_actionable_findings": review_execution.result.has_actionable_findings,
                "findings_count": review_execution.result.findings_count,
                "tests_status": review_execution.result.tests_status,
                "summary": review_execution.result.summary,
            },
        )

    return StartResult(
        run_id=run_id,
        status=state.status.value,
        chat_id=chat_id,
        iteration_dir=f"cursor/iterations/{iteration}",
        staged_diff_path=staging.artifacts.patch_path,
        result_message=result_message,
    )


def _ensure_cursor_chat(run_directory: Path, state: RunState) -> str:
    if state.cursor.chat_id:
        return state.cursor.chat_id
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


def render_start_output(result: StartResult) -> str:
    return (
        f"Started run {result.run_id}\n"
        f"Status: {result.status}\n"
        f"Cursor chat: {result.chat_id}\n"
        f"Artifacts: {result.iteration_dir}\n"
        f"Staged diff: {result.staged_diff_path}\n"
        f"{result.result_message}\n"
    )

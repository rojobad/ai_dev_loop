"""Start command: preflight, locks, Cursor chat creation, and initial execution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.commands.start_preflight import (
    begin_running_cursor,
    begin_staging,
    begin_validating,
    mark_failed,
    mark_interrupted,
    run_start_preflight_checks,
    validate_start_status,
)
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.cursor import create_chat, execute_prompt
from ai_dev_loop.runners.probes import (
    capture_git_status,
    require_probe_success,
    run_start_probes,
)
from ai_dev_loop.state import (
    RunState,
    RunStatus,
    append_run_log,
    atomic_write_json,
    atomic_write_text,
    save_run_state,
)

PHASE_2_BOUNDARY_MESSAGE = (
    "Cursor execution is complete. Git staging, Codex review, corrections, "
    "and completion are not implemented yet."
)
CODEX_TUI_WARNING = "Important: exit the active Codex TUI before continuing with start."


@dataclass(frozen=True)
class StartResult:
    run_id: str
    status: str
    chat_id: str
    iteration_dir: str
    boundary_message: str


def _fail_run(run_directory: Path, state: RunState, message: str) -> None:
    mark_failed(state, message)
    save_run_state(run_directory, state)
    append_run_log(run_directory, message)


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

        try:
            validate_start_status(state)
        except ValidationError as exc:
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

        begin_validating(state)
        save_run_state(run_directory, state)
        append_run_log(run_directory, "status=validating")

        try:
            run_start_preflight_checks(state, run_directory)
        except ValidationError as exc:
            _fail_run(run_directory, state, str(exc))
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
        append_run_log(run_directory, "preflight passed")

        try:
            probe_results = run_start_probes(
                cursor_command=state.cursor.command,
                cursor_model=state.cursor.model,
                codex_command=state.codex.command,
            )
            require_probe_success(probe_results)
        except ValidationError as exc:
            _fail_run(run_directory, state, str(exc))
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc
        append_run_log(run_directory, "local CLI probes passed")

        try:
            chat_id = _ensure_cursor_chat(run_directory, state)
        except ValidationError as exc:
            _fail_run(run_directory, state, str(exc))
            raise AiDevLoopError(str(exc), exit_code=exc.exit_code) from exc

        begin_running_cursor(state)
        save_run_state(run_directory, state)
        append_run_log(run_directory, f"Cursor chat ready: {chat_id}; status=running_cursor")

        iteration = "01"
        iteration_dir = run_directory / "cursor" / "iterations" / iteration
        iteration_dir.mkdir(parents=True, exist_ok=True)

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
            _fail_run(run_directory, state, str(exc))
            raise
        except OSError as exc:
            message = f"Cursor artifact capture failed: {exc}"
            _fail_run(run_directory, state, message)
            raise AiDevLoopError(message) from exc

        if execution.process.timed_out:
            mark_interrupted(state, "Cursor execution timed out")
            save_run_state(run_directory, state)
            append_run_log(run_directory, "Cursor execution timed out")
            raise AiDevLoopError("Cursor execution timed out")

        if execution.process.returncode != 0:
            detail = execution.process.stderr.strip() or "Cursor execution failed"
            mark_failed(state, detail)
            save_run_state(run_directory, state)
            append_run_log(
                run_directory,
                f"Cursor execution failed: exit {execution.process.returncode}",
            )
            raise AiDevLoopError(f"Cursor execution failed: {detail}")

        begin_staging(state)
        state.result = PHASE_2_BOUNDARY_MESSAGE
        state.last_error = None
        save_run_state(run_directory, state)
        append_run_log(run_directory, "Cursor execution complete; status=staging")

    return StartResult(
        run_id=run_id,
        status=RunStatus.STAGING.value,
        chat_id=chat_id,
        iteration_dir=f"cursor/iterations/{iteration}",
        boundary_message=PHASE_2_BOUNDARY_MESSAGE,
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
        f"{result.boundary_message}\n"
    )

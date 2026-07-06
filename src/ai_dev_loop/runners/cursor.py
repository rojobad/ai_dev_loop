"""Cursor chat creation, headless execution, and stream-json parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import (
    ActiveProcessRegistration,
    StreamingProcessResult,
    run_process,
    run_process_streaming,
)
from ai_dev_loop.redaction import redact_text
from ai_dev_loop.state import CursorState

CHAT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CursorParseResult:
    final_text: str | None
    errors: tuple[str, ...]
    parse_ok: bool


@dataclass(frozen=True)
class CursorExecutionResult:
    process: StreamingProcessResult
    parse: CursorParseResult
    metadata_args: list[str]


def create_chat(cursor_command: str) -> str:
    result = run_process([cursor_command, "create-chat"])
    if result.returncode != 0:
        detail = redact_text(result.stderr.strip() or result.stdout.strip() or "create-chat failed")
        raise ValidationError(f"Cursor chat creation failed: {detail}")
    chat_id = result.stdout.strip()
    if not chat_id or not CHAT_ID_PATTERN.match(chat_id):
        raise ValidationError("Cursor chat creation returned an invalid chat ID")
    return chat_id


def build_cursor_args(
    cursor: CursorState,
    *,
    repo_root: str,
    chat_id: str,
    prompt: str,
) -> list[str]:
    args = [
        cursor.command,
        "-p",
    ]
    if cursor.force:
        args.append("--force")
    if cursor.trust_workspace:
        args.append("--trust")
    args.extend(
        [
            "--workspace",
            repo_root,
            "--resume",
            chat_id,
            "--model",
            cursor.model,
            "--output-format",
            cursor.output_format,
            "--sandbox",
            cursor.sandbox,
            prompt,
        ]
    )
    return args


def redact_cursor_args(args: list[str]) -> list[str]:
    if not args:
        return []
    redacted = list(args)
    redacted[-1] = "<prompt-redacted>"
    return redacted


def execute_prompt(
    cursor: CursorState,
    *,
    repo_root: str,
    chat_id: str,
    prompt: str,
    timeout_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
    run_directory: Path | None = None,
    run_id: str | None = None,
    iteration_number: int | None = None,
) -> CursorExecutionResult:
    args = build_cursor_args(cursor, repo_root=repo_root, chat_id=chat_id, prompt=prompt)
    active_process = None
    if run_directory is not None and run_id is not None and iteration_number is not None:
        active_process = ActiveProcessRegistration(
            run_directory=run_directory,
            run_id=run_id,
            component="cursor",
            iteration=iteration_number,
            argv_redacted=redact_cursor_args(args),
        )
    process = run_process_streaming(
        args,
        cwd=repo_root,
        timeout=timeout_seconds,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        sensitive=True,
        active_process=active_process,
    )
    parse = parse_stream_json(process.stdout)
    return CursorExecutionResult(
        process=process,
        parse=parse,
        metadata_args=redact_cursor_args(args),
    )


def parse_stream_json(stdout: str) -> CursorParseResult:
    final_text: str | None = None
    errors: list[str] = []
    parse_ok = True

    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            parse_ok = False
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or event.get("event") or "")
        if event_type in {"error", "failure"}:
            message = _extract_text(event) or str(event)
            errors.append(redact_text(message))
        if event_type in {"result", "final", "assistant"}:
            text = _extract_text(event)
            if text:
                final_text = text
        if "result" in event and isinstance(event["result"], str):
            final_text = event["result"]

    return CursorParseResult(
        final_text=final_text,
        errors=tuple(errors),
        parse_ok=parse_ok and not errors,
    )


def _extract_text(event: dict[str, Any]) -> str | None:
    for key in ("result", "text", "message", "content"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None

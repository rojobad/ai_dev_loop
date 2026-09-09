"""Scheduler Codex review argv builders with frozen submit-time configuration."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.domain.codex_contract import SCHEDULER_CODEX_REVIEW_SANDBOX


def scheduler_codex_review_sandbox(configured_sandbox: str | None = None) -> str:
    """Return the enforced scheduler review sandbox, ignoring writable legacy values."""

    _ = configured_sandbox
    return SCHEDULER_CODEX_REVIEW_SANDBOX


def build_scheduler_codex_bootstrap_args(
    *,
    command: str,
    repo_root: str,
    sandbox: str,
    review_model: str,
    review_reasoning_effort: str,
    schema_file: Path,
    result_file: Path,
) -> list[str]:
    sandbox = scheduler_codex_review_sandbox(sandbox)
    if sandbox != SCHEDULER_CODEX_REVIEW_SANDBOX:
        raise ValidationError("scheduler Codex bootstrap requires read-only sandbox")
    return [
        command,
        "exec",
        "--cd",
        repo_root,
        "--sandbox",
        sandbox,
        "--model",
        review_model,
        "-c",
        f'model_reasoning_effort="{review_reasoning_effort}"',
        "--json",
        "--output-schema",
        str(schema_file),
        "--output-last-message",
        str(result_file),
        "-",
    ]


def build_scheduler_codex_resume_args(
    *,
    command: str,
    repo_root: str,
    sandbox: str,
    review_model: str,
    review_reasoning_effort: str,
    session_id: str,
    schema_file: Path,
    result_file: Path,
) -> list[str]:
    if not session_id.strip():
        raise ValidationError("scheduler Codex resume requires bound reviewer session id")
    sandbox = scheduler_codex_review_sandbox(sandbox)
    if sandbox != SCHEDULER_CODEX_REVIEW_SANDBOX:
        raise ValidationError("scheduler Codex resume requires read-only sandbox")
    return [
        command,
        "exec",
        "--cd",
        repo_root,
        "--sandbox",
        sandbox,
        "resume",
        "--model",
        review_model,
        "-c",
        f'model_reasoning_effort="{review_reasoning_effort}"',
        "--json",
        "--output-schema",
        str(schema_file),
        "--output-last-message",
        str(result_file),
        session_id,
        "-",
    ]

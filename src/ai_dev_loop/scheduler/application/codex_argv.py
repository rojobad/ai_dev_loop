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
        raise ValidationError("scheduler Codex bootstrap requires workspace-write sandbox")
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


def build_scheduler_codex_implementer_bootstrap_args(
    *,
    command: str,
    repo_root: str,
    model: str,
    reasoning_effort: str,
) -> list[str]:
    """First Codex implementer turn. No review schema and no ``--last``."""

    if not model.strip() or not reasoning_effort.strip():
        raise ValidationError("codex implementer bootstrap requires frozen model and effort")
    return [
        command,
        "exec",
        "--cd",
        repo_root,
        "--sandbox",
        SCHEDULER_CODEX_REVIEW_SANDBOX,
        "-c",
        'approval_policy="never"',
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--json",
        "-",
    ]


def build_scheduler_codex_implementer_resume_args(
    *,
    command: str,
    repo_root: str,
    model: str,
    reasoning_effort: str,
    session_id: str,
) -> list[str]:
    """Resume the exact implementer thread. Root options stay before ``resume``."""

    if not session_id.strip():
        raise ValidationError("codex implementer resume requires the bound implementer session")
    if session_id.strip() == "--last":
        raise ValidationError("codex implementer resume rejects --last")
    if not model.strip() or not reasoning_effort.strip():
        raise ValidationError("codex implementer resume requires frozen model and effort")
    return [
        command,
        "exec",
        "--cd",
        repo_root,
        "--sandbox",
        SCHEDULER_CODEX_REVIEW_SANDBOX,
        "-c",
        'approval_policy="never"',
        "resume",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--json",
        session_id,
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
        raise ValidationError("scheduler Codex resume requires workspace-write sandbox")
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

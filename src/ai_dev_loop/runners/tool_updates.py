"""Official Cursor/Codex CLI self-updater invocation with explicit consent."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from ai_dev_loop.abort_control import is_abort_requested
from ai_dev_loop.errors import UsageError, ValidationError
from ai_dev_loop.locking import FileLock, LockMetadata
from ai_dev_loop.paths import ensure_dir, set_sensitive_file_mode, state_dir
from ai_dev_loop.process import ActiveProcessRegistration, run_process, run_process_streaming
from ai_dev_loop.redaction import redact_text
from ai_dev_loop.state import atomic_write_json

DEFAULT_UPDATER_TIMEOUT_SECONDS = 300.0


class UpdateMode(StrEnum):
    """How the workflow should treat incompatible tools."""

    ASK = "ask"
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True)
class ToolUpdateFlags:
    """CLI flags for start/resume tool update consent."""

    update_tools: bool = False
    skip_tool_update: bool = False
    allow_incompatible_tools: bool = False


@dataclass(frozen=True)
class ToolUpdatePrompt:
    tool: Literal["cursor", "codex"]
    command: str
    installed_version: str | None
    required_model: str | None
    classification: str
    detail: str


@dataclass(frozen=True)
class ToolCompatibilityPolicy:
    """Library-level update policy. Never reads stdin itself."""

    update_mode: UpdateMode
    allow_incompatible: bool = False
    ask_callback: Callable[[ToolUpdatePrompt], bool] | None = None


@dataclass(frozen=True)
class UpdaterResult:
    tool: str
    command: str
    argv: list[str]
    returncode: int
    timed_out: bool
    version_before: str | None
    version_after: str | None
    artifact_path: str


def validate_tool_update_flags(flags: ToolUpdateFlags) -> None:
    if flags.update_tools and flags.skip_tool_update:
        raise UsageError(
            "contradictory flags: --update-tools and --skip-tool-update cannot be combined"
        )


def policy_from_flags(
    flags: ToolUpdateFlags,
    *,
    stdin_is_tty: bool,
    ask_callback: Callable[[ToolUpdatePrompt], bool] | None = None,
) -> ToolCompatibilityPolicy:
    validate_tool_update_flags(flags)
    if flags.update_tools:
        mode = UpdateMode.ALWAYS
    elif flags.skip_tool_update or not stdin_is_tty:
        mode = UpdateMode.NEVER
    else:
        mode = UpdateMode.ASK
    return ToolCompatibilityPolicy(
        update_mode=mode,
        allow_incompatible=flags.allow_incompatible_tools,
        ask_callback=ask_callback if mode == UpdateMode.ASK else None,
    )


def default_policy(*, stdin_is_tty: bool) -> ToolCompatibilityPolicy:
    return policy_from_flags(ToolUpdateFlags(), stdin_is_tty=stdin_is_tty)


def tool_update_lock_path() -> Path:
    return state_dir() / "locks" / "tool-update.lock"


def run_official_updater(
    *,
    tool: Literal["cursor", "codex"],
    command: str,
    version_before: str | None,
    run_directory: Path,
    run_id: str,
    timeout_seconds: float = DEFAULT_UPDATER_TIMEOUT_SECONDS,
) -> UpdaterResult:
    """Run ``<command> update`` in a process group under an XDG update lock.

    The updater is registered as an active child so ``abort`` can signal the full
    process group. The update lock is held until the group is reaped.
    """

    if is_abort_requested(run_directory):
        raise ValidationError("abort requested; refusing to launch a CLI updater")

    argv = [command, "update"]
    ensure_dir(tool_update_lock_path().parent)
    artifact_dir = run_directory / "preflight"
    ensure_dir(artifact_dir)
    stdout_path = artifact_dir / f"{tool}-update.stdout.txt"
    stderr_path = artifact_dir / f"{tool}-update.stderr.txt"
    artifact_path = artifact_dir / f"{tool}-update.json"

    lock = FileLock(tool_update_lock_path())
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=str(run_directory),
        started_at=datetime.now(tz=UTC),
    )
    lock.acquire(metadata)
    try:
        if is_abort_requested(run_directory):
            raise ValidationError("abort requested; refusing to launch a CLI updater")
        result = run_process_streaming(
            argv,
            timeout=timeout_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            sensitive=True,
            active_process=ActiveProcessRegistration(
                run_directory=run_directory,
                run_id=run_id,
                component=f"{tool}_update",
                iteration=0,
                argv_redacted=list(argv),
            ),
        )
    finally:
        lock.release()

    version_after = _probe_version_quiet(command)
    payload = {
        "tool": tool,
        "command": command,
        "argv": argv,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "version_before": version_before,
        "version_after": version_after,
        "stdout_path": str(stdout_path.relative_to(run_directory)),
        "stderr_path": str(stderr_path.relative_to(run_directory)),
        "stdout_summary": _safe_output_summary(result.stdout),
        "stderr_summary": _safe_output_summary(result.stderr),
        "completed_at": datetime.now(tz=UTC).isoformat(),
    }
    atomic_write_json(artifact_path, payload, sensitive=True)
    set_sensitive_file_mode(artifact_path)

    if result.timed_out:
        raise ValidationError(
            f"{tool} updater timed out; inspect {artifact_path.relative_to(run_directory)}"
        )
    if is_abort_requested(run_directory):
        raise ValidationError("abort requested during CLI updater")
    if result.returncode != 0:
        raise ValidationError(
            f"{tool} updater failed with exit code {result.returncode}; "
            f"inspect {artifact_path.relative_to(run_directory)}"
        )
    return UpdaterResult(
        tool=tool,
        command=command,
        argv=argv,
        returncode=result.returncode,
        timed_out=result.timed_out,
        version_before=version_before,
        version_after=version_after,
        artifact_path=str(artifact_path.relative_to(run_directory)),
    )


def manual_update_commands(*, cursor_command: str, codex_command: str) -> list[str]:
    return [f"{cursor_command} update", f"{codex_command} update"]


def _probe_version_quiet(command: str) -> str | None:
    result = run_process([command, "--version"], timeout=30.0)
    if result.returncode != 0:
        return None
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0].strip() if text else None


def _safe_output_summary(text: str, *, limit: int = 200) -> str:
    redacted = redact_text(text.strip())
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + "…"


def write_compatibility_artifact(
    run_directory: Path,
    *,
    tool: str,
    payload: dict[str, object],
) -> str:
    artifact_dir = run_directory / "preflight"
    ensure_dir(artifact_dir)
    path = artifact_dir / f"{tool}-compatibility.json"
    atomic_write_json(path, payload, sensitive=True)
    set_sensitive_file_mode(path)
    return str(path.relative_to(run_directory))


def dump_policy_debug(policy: ToolCompatibilityPolicy) -> str:
    """Test helper: stable representation without callbacks."""

    return json.dumps(
        {
            "update_mode": policy.update_mode.value,
            "allow_incompatible": policy.allow_incompatible,
            "has_ask_callback": policy.ask_callback is not None,
        },
        sort_keys=True,
    )

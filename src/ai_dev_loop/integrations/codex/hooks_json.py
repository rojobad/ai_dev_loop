"""Safe merge and uninstall logic for ~/.codex/hooks.json."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.assets import (
    HOOK_MATCHER,
    HOOK_SCRIPT_NAME,
    HOOK_STATUS_MESSAGE,
)
from ai_dev_loop.state import atomic_write_json


@dataclass(frozen=True)
class HookRegistrationStatus:
    present: bool
    duplicate_count: int
    command: str | None


def is_ai_dev_loop_hook_command(command: str, *, expected_command: str) -> bool:
    return command == expected_command


def is_managed_hook_command(command: str, *, hook_script: Path, hook: dict[str, Any]) -> bool:
    if command == build_hook_command(hook_script):
        return True
    if hook.get("statusMessage") != HOOK_STATUS_MESSAGE:
        return False
    canonical_suffix = f"/.codex/hooks/{HOOK_SCRIPT_NAME}"
    return command.startswith("python3 ") and command.endswith(canonical_suffix)


def _command_is_owned(
    hook: dict[str, Any], *, hook_script: Path, managed: bool, expected_command: str
) -> bool:
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    if is_ai_dev_loop_hook_command(command, expected_command=expected_command):
        return True
    if not managed:
        return False
    return is_managed_hook_command(command, hook_script=hook_script, hook=hook)


def build_hook_command(hook_script: Path) -> str:
    return f"python3 {hook_script.resolve()}"


def load_hooks_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"Invalid hooks.json at {path}: {exc}. "
            "Fix or restore the file before installing or uninstalling ai_dev_loop integrations."
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"Invalid hooks.json at {path}: root value must be a JSON object.")
    return payload


def backup_hooks_json(path: Path) -> Path | None:
    if not path.is_file():
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.backup.{timestamp}")
    backup_path.write_bytes(path.read_bytes())
    return backup_path


def _iter_session_start_entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValidationError("Invalid hooks.json: hooks must be an object.")
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        raise ValidationError("Invalid hooks.json: hooks.SessionStart must be an array.")
    return entries


def _remove_ai_dev_loop_hooks_from_entry(
    entry: dict[str, Any], *, hook_script: Path, managed: bool, expected_command: str
) -> tuple[dict[str, Any], int]:
    if not isinstance(entry, dict):
        return entry, 0
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return entry, 0
    removed = 0
    kept: list[dict[str, Any]] = []
    for hook in hooks:
        if not isinstance(hook, dict):
            kept.append(hook)
            continue
        if _command_is_owned(
            hook,
            hook_script=hook_script,
            managed=managed,
            expected_command=expected_command,
        ):
            removed += 1
            continue
        kept.append(hook)
    updated = copy.deepcopy(entry)
    updated["hooks"] = kept
    return updated, removed


def remove_all_ai_dev_loop_hooks(
    document: dict[str, Any], *, hook_script: Path, managed: bool = False
) -> tuple[dict[str, Any], int]:
    expected_command = build_hook_command(hook_script)
    updated = copy.deepcopy(document)
    entries = _iter_session_start_entries(updated)
    removed_total = 0
    cleaned_entries: list[dict[str, Any]] = []
    for entry in entries:
        cleaned, removed = _remove_ai_dev_loop_hooks_from_entry(
            entry,
            hook_script=hook_script,
            managed=managed,
            expected_command=expected_command,
        )
        removed_total += removed
        hooks = cleaned.get("hooks")
        if (isinstance(hooks, list) and hooks) or not isinstance(hooks, list):
            cleaned_entries.append(cleaned)
    updated["hooks"]["SessionStart"] = cleaned_entries
    return updated, removed_total


def registration_status(
    document: dict[str, Any], *, expected_command: str | None = None
) -> HookRegistrationStatus:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return HookRegistrationStatus(present=False, duplicate_count=0, command=None)
    entries = hooks.get("SessionStart")
    if not isinstance(entries, list):
        return HookRegistrationStatus(present=False, duplicate_count=0, command=None)

    commands: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hook_list = entry.get("hooks")
        if not isinstance(hook_list, list):
            continue
        for hook in hook_list:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command")
            if (
                isinstance(command, str)
                and expected_command is not None
                and is_ai_dev_loop_hook_command(command, expected_command=expected_command)
            ):
                commands.append(command)

    if not commands:
        return HookRegistrationStatus(present=False, duplicate_count=0, command=None)

    duplicate_count = max(0, len(commands) - 1)
    command = commands[0]
    if expected_command is not None and expected_command in commands:
        command = expected_command
    return HookRegistrationStatus(present=True, duplicate_count=duplicate_count, command=command)


def _desired_hook_entry(command: str) -> dict[str, str]:
    return {
        "type": "command",
        "command": command,
        "statusMessage": HOOK_STATUS_MESSAGE,
    }


def merge_hook_registration(
    document: dict[str, Any], *, hook_script: Path
) -> tuple[dict[str, Any], bool]:
    command = build_hook_command(hook_script)
    before = json.dumps(document, sort_keys=True)
    hook_entry = _desired_hook_entry(command)

    updated, _ = remove_all_ai_dev_loop_hooks(document, hook_script=hook_script, managed=True)
    entries = _iter_session_start_entries(updated)

    target_entry = next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("matcher") == HOOK_MATCHER
        ),
        None,
    )
    if target_entry is None:
        entries.append({"matcher": HOOK_MATCHER, "hooks": [hook_entry]})
    else:
        hooks = target_entry.get("hooks")
        if not isinstance(hooks, list):
            raise ValidationError("Invalid hooks.json: SessionStart entry hooks must be an array.")
        target_entry["hooks"] = [hook_entry, *hooks]

    changed = json.dumps(updated, sort_keys=True) != before
    return updated, changed


def write_hooks_document(path: Path, document: dict[str, Any], *, backup: bool) -> Path | None:
    backup_path = backup_hooks_json(path) if backup and path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, document, sensitive=True)
    return backup_path

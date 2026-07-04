"""Integration status command (install/uninstall deferred to later phases)."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.errors import NotImplementedCommandError


def _skill_path() -> Path:
    return Path.home() / ".agents" / "skills" / "ai-dev-loop-handoff" / "SKILL.md"


def _hook_script_path() -> Path:
    return Path.home() / ".codex" / "hooks" / "ai_dev_loop_session_start.py"


def _hooks_json_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def render_integrations_status(*, output: str = "text") -> str:
    skill_installed = _skill_path().is_file()
    hook_script_installed = _hook_script_path().is_file()
    hooks_json_exists = _hooks_json_path().is_file()
    payload = {
        "schema_version": 1,
        "skill_installed": skill_installed,
        "skill_path": str(_skill_path()),
        "hook_script_installed": hook_script_installed,
        "hook_script_path": str(_hook_script_path()),
        "hooks_json_exists": hooks_json_exists,
        "hooks_json_path": str(_hooks_json_path()),
        "note": "Global integration installation is not implemented in Phase 1.",
    }
    if output == "json":
        return json.dumps(payload, indent=2) + "\n"
    lines = [
        "ai_dev_loop integrations status",
        f"Skill installed: {skill_installed} ({_skill_path()})",
        f"Hook script installed: {hook_script_installed} ({_hook_script_path()})",
        f"hooks.json present: {hooks_json_exists} ({_hooks_json_path()})",
        "Install/uninstall commands are not implemented in Phase 1.",
    ]
    return "\n".join(lines) + "\n"


def raise_not_implemented(action: str) -> None:
    raise NotImplementedCommandError(
        f"integrations {action} is not implemented in Phase 1. "
        "Only integrations status is available."
    )

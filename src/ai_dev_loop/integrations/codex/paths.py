"""Installed path helpers for Codex global integrations."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.integrations.codex.assets import HOOK_SCRIPT_NAME, SKILL_DIRECTORY_NAME
from ai_dev_loop.paths import state_dir


def skill_path(home: Path | None = None) -> Path:
    """Return the handoff skill path under a user profile root."""

    root = home if home is not None else Path.home()
    return root / ".agents" / "skills" / SKILL_DIRECTORY_NAME / "SKILL.md"


def codex_home(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".codex"


def skill_directory(home: Path | None = None) -> Path:
    return skill_path(home).parent


def hook_script_path(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".codex" / "hooks" / HOOK_SCRIPT_NAME


def hooks_json_path(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".codex" / "hooks.json"


def codex_sessions_dir() -> Path:
    return state_dir() / "codex-sessions"

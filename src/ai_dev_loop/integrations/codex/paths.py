"""Installed path helpers for Codex global integrations."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.integrations.codex.assets import (
    HOOK_SCRIPT_NAME,
    SKILL_DIRECTORY_NAME,
    SkillDescriptor,
    skill_descriptor,
)
from ai_dev_loop.paths import state_dir


def skill_path(
    home: Path | None = None,
    *,
    directory_name: str = SKILL_DIRECTORY_NAME,
) -> Path:
    """Return a package-owned skill path under a user profile root."""

    root = home if home is not None else Path.home()
    return root / ".agents" / "skills" / directory_name / "SKILL.md"


def skill_path_for(descriptor: SkillDescriptor, home: Path | None = None) -> Path:
    return skill_path(home, directory_name=descriptor.directory_name)


def owned_skill_paths(home: Path | None = None) -> dict[str, Path]:
    from ai_dev_loop.integrations.codex.assets import OWNED_SKILLS

    return {
        descriptor.directory_name: skill_path_for(descriptor, home) for descriptor in OWNED_SKILLS
    }


def codex_home(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".codex"


def skill_directory(
    home: Path | None = None,
    *,
    directory_name: str = SKILL_DIRECTORY_NAME,
) -> Path:
    return skill_path(home, directory_name=directory_name).parent


def hook_script_path(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".codex" / "hooks" / HOOK_SCRIPT_NAME


def hooks_json_path(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / ".codex" / "hooks.json"


def codex_sessions_dir() -> Path:
    return state_dir() / "codex-sessions"


def resolve_skill_descriptor(directory_name: str) -> SkillDescriptor:
    return skill_descriptor(directory_name)

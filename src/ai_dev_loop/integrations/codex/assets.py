"""Load package-owned Codex integration assets."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

HOOK_SCRIPT_NAME = "ai_dev_loop_session_start.py"
HOOK_SOURCE_NAME = "session_start.py"
SKILL_RESOURCE = "SKILL.md"
HOOK_STATUS_MESSAGE = "Loading ai_dev_loop session context"
HOOK_MATCHER = "startup|resume|clear|compact"

# Backward-compatible alias for the primary handoff skill directory.
SKILL_DIRECTORY_NAME = "ai-dev-loop-handoff"
CONTROLLER_SKILL_DIRECTORY_NAME = "ai-dev-loop-controller"


@dataclass(frozen=True)
class SkillDescriptor:
    """Package-owned skill installation identity."""

    directory_name: str
    package_subdir: str
    frontmatter_name: str

    @property
    def package_resource_root(self) -> str:
        return f"ai_dev_loop.integrations.codex.{self.package_subdir}"


OWNED_SKILLS: tuple[SkillDescriptor, ...] = (
    SkillDescriptor(
        directory_name=SKILL_DIRECTORY_NAME,
        package_subdir="skill",
        frontmatter_name="ai-dev-loop-handoff",
    ),
    SkillDescriptor(
        directory_name=CONTROLLER_SKILL_DIRECTORY_NAME,
        package_subdir="controller_skill",
        frontmatter_name="ai-dev-loop-controller",
    ),
)

HANDOFF_SKILL = OWNED_SKILLS[0]
CONTROLLER_SKILL = OWNED_SKILLS[1]


@lru_cache(maxsize=1)
def package_root() -> Path:
    return Path(__file__).resolve().parent


def skill_descriptor(directory_name: str) -> SkillDescriptor:
    for descriptor in OWNED_SKILLS:
        if descriptor.directory_name == directory_name:
            return descriptor
    raise KeyError(f"unknown package-owned skill: {directory_name}")


def skill_package_path(descriptor: SkillDescriptor | None = None) -> Path:
    chosen = descriptor or HANDOFF_SKILL
    return package_root() / chosen.package_subdir / SKILL_RESOURCE


def hook_script_package_path() -> Path:
    return package_root() / HOOK_SOURCE_NAME


def load_skill_content(descriptor: SkillDescriptor | None = None) -> str:
    chosen = descriptor or HANDOFF_SKILL
    return (
        resources.files(chosen.package_resource_root)
        .joinpath(SKILL_RESOURCE)
        .read_text(encoding="utf-8")
    )


def load_all_skill_contents() -> dict[str, str]:
    return {
        descriptor.directory_name: load_skill_content(descriptor) for descriptor in OWNED_SKILLS
    }


def load_hook_script_content() -> str:
    return (
        resources.files("ai_dev_loop.integrations.codex")
        .joinpath(HOOK_SOURCE_NAME)
        .read_text(encoding="utf-8")
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    return sha256_bytes(path.read_bytes())


def content_matches_package(path: Path, *, expected_text: str) -> bool:
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == expected_text

"""Load package-owned Codex integration assets."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import resources
from pathlib import Path

HOOK_SCRIPT_NAME = "ai_dev_loop_session_start.py"
HOOK_SOURCE_NAME = "session_start.py"
SKILL_RESOURCE = "SKILL.md"
HOOK_STATUS_MESSAGE = "Loading ai_dev_loop session context"
HOOK_MATCHER = "startup|resume|clear|compact"
SKILL_DIRECTORY_NAME = "ai-dev-loop-handoff"


@lru_cache(maxsize=1)
def package_root() -> Path:
    return Path(__file__).resolve().parent


def skill_package_path() -> Path:
    return package_root() / "skill" / SKILL_RESOURCE


def hook_script_package_path() -> Path:
    return package_root() / HOOK_SOURCE_NAME


def load_skill_content() -> str:
    return (
        resources.files("ai_dev_loop.integrations.codex.skill")
        .joinpath(SKILL_RESOURCE)
        .read_text(encoding="utf-8")
    )


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

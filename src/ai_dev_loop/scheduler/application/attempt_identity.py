"""Attempt identity, unit naming, and worktree lock path helpers."""

from __future__ import annotations

import re
from pathlib import Path

_ATTEMPT_ID_RE = re.compile(r"^att-[0-9a-f]{32}$")
_UNIT_IDENTITY_RE = re.compile(r"^ai-dev-loop-attempt-att-[0-9a-f]{32}\.service$")
_WORKTREE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

UNIT_IDENTITY_PREFIX = "ai-dev-loop-attempt-"
UNIT_IDENTITY_SUFFIX = ".service"
WORKTREE_LOCKS_DIRNAME = "worktree-locks"


def validate_attempt_id(attempt_id: str) -> str:
    if not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise ValueError("attempt_id must match att-<32 lowercase hex>")
    return attempt_id


def unit_identity_from_attempt_id(attempt_id: str) -> str:
    safe_id = validate_attempt_id(attempt_id)
    unit_identity = f"{UNIT_IDENTITY_PREFIX}{safe_id}{UNIT_IDENTITY_SUFFIX}"
    validate_unit_identity(unit_identity)
    return unit_identity


def validate_unit_identity(unit_identity: str) -> str:
    if not _UNIT_IDENTITY_RE.fullmatch(unit_identity):
        raise ValueError("unit_identity must be a safe systemd transient unit name")
    return unit_identity


def worktree_lock_path(state_root: Path, worktree_key: str) -> Path:
    if not _WORKTREE_KEY_RE.fullmatch(worktree_key):
        raise ValueError("worktree_key must be 64 lowercase hex characters")
    return state_root / WORKTREE_LOCKS_DIRNAME / f"{worktree_key}.lock"


def flock_wrapped_argv(lock_path: Path, agent_argv: list[str]) -> list[str]:
    if not agent_argv:
        raise ValueError("agent_argv must not be empty")
    lock_text = str(lock_path)
    if not lock_text or lock_text.startswith("-") or any(ch in lock_text for ch in (";", "&", "|")):
        raise ValueError("lock_path is not safe for argv embedding")
    return ["flock", "--exclusive", "--nonblock", lock_text, *agent_argv]

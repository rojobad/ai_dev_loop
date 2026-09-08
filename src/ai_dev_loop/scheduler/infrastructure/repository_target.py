"""Side-effect-free repository target resolution for scheduler submit."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError


@dataclass(frozen=True)
class RepositoryTarget:
    """Canonical worktree root for scheduler submission."""

    root: Path


def resolve_repository_target(repo_path: Path) -> RepositoryTarget:
    """Locate a git worktree root by walking upward for a .git marker only."""

    return RepositoryTarget(root=_find_worktree_root(repo_path))


def _find_worktree_root(start: Path) -> Path:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        dot_git = current / ".git"
        if dot_git.exists():
            if dot_git.is_dir():
                return current
            if dot_git.is_file():
                text = dot_git.read_text(encoding="utf-8").strip()
                if not text.startswith("gitdir:"):
                    raise ValidationError(f"invalid .git file marker under {current}")
                return current
        if current.parent == current:
            break
        current = current.parent
    raise ValidationError(f"repository path is not a git worktree: {start}")

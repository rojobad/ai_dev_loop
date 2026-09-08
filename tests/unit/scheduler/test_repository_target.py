"""Unit tests for repository target resolution at scheduler submit."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.domain.common import worktree_key
from ai_dev_loop.scheduler.infrastructure.repository_target import (
    RepositoryTarget,
    resolve_repository_target,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_resolve_repository_target_from_repo_root(git_repo: Path) -> None:
    target = resolve_repository_target(git_repo)
    assert target.root == git_repo.resolve()


def test_resolve_repository_target_from_nested_path(git_repo: Path) -> None:
    nested = git_repo / "docs" / "plans"
    nested.mkdir(parents=True, exist_ok=True)
    target = resolve_repository_target(nested)
    assert target.root == git_repo.resolve()


def test_resolve_repository_target_supports_linked_worktree(git_repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked-worktree"
    _git(git_repo, "worktree", "add", str(linked), "-b", "linked-branch")
    target = resolve_repository_target(linked)
    assert target.root == linked.resolve()


def test_resolve_repository_target_rejects_non_repo(tmp_path: Path) -> None:
    isolated = tmp_path / "no-git-marker"
    isolated.mkdir()
    current = isolated.resolve()
    while current.parent != current:
        if (current / ".git").exists():
            pytest.skip("temp directory is inside a git worktree")
        current = current.parent
    with pytest.raises(ValidationError, match="not a git worktree"):
        resolve_repository_target(isolated)


def test_resolve_repository_target_rejects_invalid_git_file(tmp_path: Path) -> None:
    repo = tmp_path / "invalid-marker"
    repo.mkdir()
    (repo / ".git").write_text("not-a-gitdir-marker\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid .git file marker"):
        resolve_repository_target(repo)


def test_worktree_key_is_stable_for_canonical_root(git_repo: Path) -> None:
    target = resolve_repository_target(git_repo / "docs")
    expected = worktree_key(str(git_repo.resolve()))
    assert worktree_key(str(target.root)) == expected


def test_resolve_repository_target_does_not_invoke_subprocess(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("repository target resolution must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    resolve_repository_target(git_repo)


def test_resolve_repository_target_does_not_read_git_directory_internals(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_dir = git_repo / ".git"

    original_is_file = Path.is_file
    original_is_dir = Path.is_dir
    original_exists = Path.exists
    original_read_text = Path.read_text

    def guarded_is_file(self: Path) -> bool:
        if self.parent == git_dir or self == git_dir:
            raise AssertionError(f"repository target must not inspect git dir file: {self}")
        return original_is_file(self)

    def guarded_is_dir(self: Path) -> bool:
        if self != git_dir and self.parent == git_dir:
            raise AssertionError(f"repository target must not inspect git dir file: {self}")
        return original_is_dir(self)

    def guarded_exists(self: Path) -> bool:
        if self.parent == git_dir and self != git_dir:
            raise AssertionError(f"repository target must not inspect git dir file: {self}")
        return original_exists(self)

    def guarded_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.parent == git_dir:
            raise AssertionError(f"repository target must not read git dir file: {self}")
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    monkeypatch.setattr(Path, "exists", guarded_exists)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    with patch.object(Path, "read_text", guarded_read_text):
        target = resolve_repository_target(git_repo)
    assert isinstance(target, RepositoryTarget)

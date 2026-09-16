"""Managed worktree tests for Phase 20.6."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ai_dev_loop.runners.git import (
    _checkpoint_git_success,
    checkpoint_git_diff_cached_patch_bytes,
    checkpoint_git_write_tree,
    recovery_git_apply_staged_patch,
    recovery_git_create_private_ref,
    recovery_git_worktree_add,
)


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "x@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    tracked = path / "a.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_worktree_seed_applies_exact_patch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    target = repo / "a.txt"
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    patch = checkpoint_git_diff_cached_patch_bytes(repo)
    subprocess.run(
        ["git", "restore", "--staged", "a.txt"], cwd=repo, check=True, capture_output=True
    )
    target.write_text("base\n", encoding="utf-8")

    worktree = tmp_path / "recovery-wt"
    private_ref = "refs/ai-dev-loop/recovery/" + ("a" * 64)
    recovery_git_create_private_ref(repo, ref=private_ref, parent_head=head)
    recovery_git_worktree_add(
        repo,
        worktree_path=worktree,
        parent_head=head,
    )
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    live_patch = checkpoint_git_diff_cached_patch_bytes(worktree)
    assert live_patch == patch
    assert checkpoint_git_write_tree(worktree) != checkpoint_git_write_tree(repo)


def test_worktree_seed_resumes_after_index_only_patch_application(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    target = repo / "a.txt"
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    patch = checkpoint_git_diff_cached_patch_bytes(repo)
    subprocess.run(
        ["git", "restore", "--staged", "a.txt"], cwd=repo, check=True, capture_output=True
    )
    target.write_text("base\n", encoding="utf-8")

    worktree = tmp_path / "recovery-wt"
    private_ref = "refs/ai-dev-loop/recovery/" + ("b" * 64)
    recovery_git_create_private_ref(repo, ref=private_ref, parent_head=head)
    recovery_git_worktree_add(
        repo,
        worktree_path=worktree,
        parent_head=head,
    )
    import tempfile

    with tempfile.NamedTemporaryFile(
        prefix=".ai-dev-loop-recovery-patch-",
        suffix=".patch",
        dir=worktree,
        delete=False,
    ) as handle:
        patch_path = Path(handle.name)
        handle.write(patch)
    _checkpoint_git_success(
        ["apply", "--whitespace=nowarn", "--cached", str(patch_path)],
        cwd=worktree,
        context="recovery git apply",
    )
    patch_path.unlink(missing_ok=True)
    assert checkpoint_git_diff_cached_patch_bytes(worktree) == patch
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "base\n"

    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    assert checkpoint_git_diff_cached_patch_bytes(worktree) == patch
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "changed\n"
    assert checkpoint_git_write_tree(worktree) != checkpoint_git_write_tree(repo)

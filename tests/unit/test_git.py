"""Unit tests for Git discovery and worktree safety."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.config import resolve_repo_config_path
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    _extract_status_path,
    discover_repository,
    is_path_within,
    resolve_repo_relative_path,
    validate_clean_worktree,
)


def test_extract_porcelain_v2_tracked_change_path() -> None:
    line = "1 .M N... 100644 100644 100644 abc123 abc123 tracked.txt"
    assert _extract_status_path(line) == "tracked.txt"


def test_extract_porcelain_v2_rename_path() -> None:
    line = "2 R. N... 100644 100644 100644 abc123 abc123 R100 new.txt\told.txt"
    assert _extract_status_path(line) == "new.txt"


def test_extract_porcelain_v2_unmerged_path() -> None:
    line = "u UU N... 100644 100644 100644 100644 abc123 abc123 abc123 conflict.txt"
    assert _extract_status_path(line) == "conflict.txt"


def test_discover_repository(git_repo: Path) -> None:
    info = discover_repository(git_repo)
    assert info.root == git_repo.resolve()
    assert info.branch == "main" or info.branch == "master"
    assert len(info.head) == 40


def test_path_containment(git_repo: Path) -> None:
    inside = resolve_repo_relative_path(git_repo, Path("docs/plans/sample-plan.md"))
    assert is_path_within(inside, git_repo)


def test_reject_path_outside_repo(git_repo: Path) -> None:
    with pytest.raises(ValidationError):
        resolve_repo_relative_path(git_repo, Path("/etc/passwd"))


def test_reject_external_config_path(git_repo: Path, tmp_path: Path) -> None:
    external = tmp_path / "ai_dev_loop.yaml"
    external.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="escapes repository"):
        resolve_repo_config_path(git_repo, external)


def test_reject_staged_changes(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    dirty = git_repo / "dirty.txt"
    dirty.write_text("dirty", encoding="utf-8")
    subprocess.run(["git", "add", "dirty.txt"], cwd=git_repo, check=True)
    info = discover_repository(git_repo)
    with pytest.raises(ValidationError, match="staged"):
        validate_clean_worktree(
            info,
            plan_path=plan,
            prompt_source_path=prompt,
            repo_root=git_repo,
            require_clean=True,
        )


def test_reject_unrelated_dirty_file(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    (git_repo / "unexpected.txt").write_text("x", encoding="utf-8")
    info = discover_repository(git_repo)
    with pytest.raises(ValidationError, match="not clean"):
        validate_clean_worktree(
            info,
            plan_path=plan,
            prompt_source_path=prompt,
            repo_root=git_repo,
            require_clean=True,
        )


def test_reject_modified_tracked_file(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    tracked = git_repo / "ai_dev_loop.yaml"
    tracked.write_text(tracked.read_text(encoding="utf-8") + "\n# touched\n", encoding="utf-8")
    info = discover_repository(git_repo)
    with pytest.raises(ValidationError, match="not clean"):
        validate_clean_worktree(
            info,
            plan_path=plan,
            prompt_source_path=prompt,
            repo_root=git_repo,
            require_clean=True,
        )


def test_allow_plan_changes(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    plan.write_text("# updated plan\n", encoding="utf-8")
    info = discover_repository(git_repo)
    validate_clean_worktree(
        info,
        plan_path=plan,
        prompt_source_path=prompt,
        repo_root=git_repo,
        require_clean=True,
    )


def test_reject_tracked_dirty_prompt(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    prompt.write_text("updated tracked prompt\n", encoding="utf-8")
    info = discover_repository(git_repo)
    with pytest.raises(ValidationError, match="not clean"):
        validate_clean_worktree(
            info,
            plan_path=plan,
            prompt_source_path=prompt,
            repo_root=git_repo,
            require_clean=True,
        )


def test_reject_tracked_prompt_matching_gitignore(git_repo: Path) -> None:
    """Tracked prompts that match .gitignore must not bypass clean-worktree checks."""
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    gitignore = git_repo / ".gitignore"
    gitignore.write_text("docs/plans/prompt_*.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "ignore prompts"], cwd=git_repo, check=True)
    assert (
        subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "--", "docs/plans/prompt_sample-plan.txt"],
            cwd=git_repo,
        ).returncode
        == 0
    )
    prompt.write_text("dirty tracked prompt despite gitignore\n", encoding="utf-8")
    info = discover_repository(git_repo)
    with pytest.raises(ValidationError, match="not clean"):
        validate_clean_worktree(
            info,
            plan_path=plan,
            prompt_source_path=prompt,
            repo_root=git_repo,
            require_clean=True,
        )


def test_allow_ignored_dirty_prompt(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    gitignore = git_repo / ".gitignore"
    gitignore.write_text("docs/plans/prompt_*.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "ignore prompts"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "rm", "--cached", "docs/plans/prompt_sample-plan.txt"],
        cwd=git_repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-m", "untrack prompt"], cwd=git_repo, check=True)
    prompt.write_text("updated ignored prompt\n", encoding="utf-8")
    info = discover_repository(git_repo)
    validate_clean_worktree(
        info,
        plan_path=plan,
        prompt_source_path=prompt,
        repo_root=git_repo,
        require_clean=True,
    )

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
    git_diff_cached_name_only,
    is_path_within,
    paths_with_index_changes,
    paths_with_worktree_changes,
    resolve_repo_relative_path,
    staged_paths_from_name_only,
    validate_clean_worktree,
    validate_no_preexisting_staged_paths,
    validate_plan_hash_unchanged,
    validate_prompt_source_unchanged,
    validate_stage_mode,
    validate_staged_paths_safe,
)
from ai_dev_loop.state import sha256_file


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


def test_validate_stage_mode_rejects_unknown() -> None:
    with pytest.raises(ValidationError, match="unsupported stage_mode"):
        validate_stage_mode("partial")


def test_validate_no_preexisting_staged_paths() -> None:
    with pytest.raises(ValidationError, match="pre-existing staged"):
        validate_no_preexisting_staged_paths(("dirty.txt",))


def test_paths_with_index_changes_detects_staged(git_repo: Path) -> None:
    dirty = git_repo / "staged.txt"
    dirty.write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=git_repo, check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "staged.txt" in paths_with_index_changes(status)


def test_paths_with_worktree_changes_includes_untracked(git_repo: Path) -> None:
    (git_repo / "new.txt").write_text("x", encoding="utf-8")
    status = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "new.txt" in paths_with_worktree_changes(status)


def test_staged_paths_from_name_only() -> None:
    assert staged_paths_from_name_only("a.txt\nb.txt\n") == ("a.txt", "b.txt")


def test_validate_prompt_source_unchanged_rejects_tracked_modification(git_repo: Path) -> None:
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    prompt.write_text("changed prompt\n", encoding="utf-8")
    status = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    with pytest.raises(ValidationError, match="prompt source"):
        validate_prompt_source_unchanged(
            status,
            "docs/plans/prompt_sample-plan.txt",
            repo_root=git_repo,
            prompt_source_path=prompt,
        )


def test_validate_staged_paths_safe_rejects_empty() -> None:
    with pytest.raises(ValidationError, match="no staged changes"):
        validate_staged_paths_safe(
            (),
            plan_repo_path="docs/plans/sample-plan.md",
            prompt_repo_path="docs/plans/prompt_sample-plan.txt",
            plan_hash="a" * 64,
            repo_root=Path("/tmp/unused"),
        )


def test_validate_staged_paths_safe_rejects_prompt_in_index(git_repo: Path) -> None:
    prompt = git_repo / "docs/plans/prompt_sample-plan.txt"
    prompt.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/plans/prompt_sample-plan.txt"], cwd=git_repo, check=True)
    with pytest.raises(ValidationError, match="must not be staged"):
        validate_staged_paths_safe(
            ("docs/plans/prompt_sample-plan.txt",),
            plan_repo_path="docs/plans/sample-plan.md",
            prompt_repo_path="docs/plans/prompt_sample-plan.txt",
            plan_hash=sha256_file(git_repo / "docs/plans/sample-plan.md"),
            repo_root=git_repo,
        )


def test_validate_plan_hash_unchanged(git_repo: Path) -> None:
    plan = git_repo / "docs/plans/sample-plan.md"
    expected = sha256_file(plan)
    validate_plan_hash_unchanged(git_repo, "docs/plans/sample-plan.md", expected)
    plan.write_text("# changed\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="plan file hash"):
        validate_plan_hash_unchanged(git_repo, "docs/plans/sample-plan.md", expected)


def test_git_diff_cached_name_only(git_repo: Path) -> None:
    target = git_repo / "ai_dev_loop.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    names = git_diff_cached_name_only(git_repo)
    assert "ai_dev_loop.yaml" in names

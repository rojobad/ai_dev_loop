"""Git discovery, path containment, and baseline worktree safety checks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import ProcessResult, require_success, run_process


@dataclass(frozen=True)
class GitRepositoryInfo:
    root: Path
    git_common_dir: Path
    git_dir: Path
    branch: str
    head: str
    status_porcelain: str
    staged_paths: tuple[str, ...]


def _git(args: list[str], *, cwd: Path) -> ProcessResult:
    return run_process(["git", *args], cwd=str(cwd))


def discover_repository(repo_path: Path) -> GitRepositoryInfo:
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise ValidationError(f"repository path does not exist: {repo_path}")

    root = Path(
        require_success(_git(["rev-parse", "--show-toplevel"], cwd=repo_path), context="git root")
    ).resolve()
    git_common_dir = Path(
        require_success(
            _git(["rev-parse", "--git-common-dir"], cwd=repo_path), context="git common dir"
        )
    )
    if not git_common_dir.is_absolute():
        git_common_dir = (root / git_common_dir).resolve()
    git_dir = Path(
        require_success(_git(["rev-parse", "--git-dir"], cwd=repo_path), context="git dir")
    )
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    branch = require_success(
        _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path), context="git branch"
    )
    head = require_success(_git(["rev-parse", "HEAD"], cwd=repo_path), context="git head")
    status = require_success(
        _git(["status", "--porcelain=v2", "--untracked-files=all"], cwd=repo_path),
        context="git status",
    )
    staged_output = require_success(
        _git(["diff", "--cached", "--name-only"], cwd=repo_path),
        context="git staged diff",
    )
    staged_paths = tuple(line.strip() for line in staged_output.splitlines() if line.strip())
    return GitRepositoryInfo(
        root=root,
        git_common_dir=git_common_dir,
        git_dir=git_dir,
        branch=branch,
        head=head,
        status_porcelain=status,
        staged_paths=staged_paths,
    )


def resolve_repo_relative_path(repo_root: Path, candidate: Path) -> Path:
    repo_root = repo_root.resolve()
    resolved = candidate.expanduser()
    if not resolved.is_absolute():
        resolved = (repo_root / resolved).resolve()
    else:
        resolved = resolved.resolve()

    if not is_path_within(resolved, repo_root):
        raise ValidationError(f"path escapes repository root: {candidate}")
    if _is_symlink_escape(candidate, repo_root):
        raise ValidationError(f"symlink escape rejected: {candidate}")
    return resolved


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_symlink_escape(candidate: Path, repo_root: Path) -> bool:
    repo_root = repo_root.resolve()
    current = candidate
    parts: list[Path] = []
    while True:
        parts.append(current)
        if current == repo_root or current.parent == current:
            break
        current = current.parent

    for part in reversed(parts):
        if part.is_symlink():
            target = part.resolve()
            if not is_path_within(target, repo_root):
                return True
    return False


def relative_repo_path(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def is_git_tracked(path: Path, repo_root: Path) -> bool:
    relative = relative_repo_path(repo_root, path)
    result = run_process(
        ["git", "ls-files", "--error-unmatch", "--", relative],
        cwd=str(repo_root),
    )
    return result.returncode == 0


def is_git_ignored(path: Path, repo_root: Path) -> bool:
    """Return True when path is git-ignored and not tracked in the index."""
    if is_git_tracked(path, repo_root):
        return False
    relative = relative_repo_path(repo_root, path)
    result = run_process(
        ["git", "check-ignore", "-q", "--", relative],
        cwd=str(repo_root),
    )
    return result.returncode == 0


def validate_clean_worktree(
    info: GitRepositoryInfo,
    *,
    plan_path: Path,
    prompt_source_path: Path,
    repo_root: Path,
    require_clean: bool,
) -> None:
    if not require_clean:
        return

    if info.staged_paths:
        raise ValidationError(
            "worktree has pre-existing staged changes; commit or unstage before prepare"
        )

    allowed_paths = {relative_repo_path(repo_root, plan_path)}
    if is_git_ignored(prompt_source_path, repo_root):
        allowed_paths.add(relative_repo_path(repo_root, prompt_source_path))

    violations = _collect_worktree_violations(info.status_porcelain, allowed_paths)
    if violations:
        joined = ", ".join(sorted(violations))
        raise ValidationError(
            f"worktree is not clean; unrelated dirty or untrusted files present: {joined}"
        )


def _collect_worktree_violations(status: str, allowed_paths: set[str]) -> set[str]:
    violations: set[str] = set()
    for line in status.splitlines():
        if not line:
            continue
        path = _extract_status_path(line)
        if path is None:
            continue
        if path in allowed_paths:
            continue
        violations.add(path)
    return violations


def _unquote_git_path(path: str) -> str:
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return bytes(path[1:-1], "utf-8").decode("unicode_escape")
    return path


def _extract_status_path(line: str) -> str | None:
    if line.startswith("?"):
        return _unquote_git_path(line[2:].strip()) or None
    if line.startswith("1 "):
        # Ordinary change: 1 XY sub mH mI mW hH hI <path>
        parts = line.split(" ", 8)
        if len(parts) >= 9:
            return _unquote_git_path(parts[8].strip())
        return None
    if line.startswith("2 "):
        # Rename/copy: path and origPath are tab-separated when both present.
        if "\t" in line:
            head, _orig = line.split("\t", 1)
            path_token = head.rsplit(" ", 1)[-1]
            return _unquote_git_path(path_token.strip())
        parts = line.split(" ", 9)
        if len(parts) >= 10:
            return _unquote_git_path(parts[9].strip())
        return None
    if line.startswith("u "):
        # Unmerged: u XY sub m1 m2 m3 mW h1 h2 h3 <path>
        parts = line.split(" ", 10)
        if len(parts) >= 11:
            return _unquote_git_path(parts[10].strip())
        return None
    return None


def copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    temp = destination.with_suffix(destination.suffix + ".tmp")
    temp.write_bytes(data)
    os.replace(temp, destination)

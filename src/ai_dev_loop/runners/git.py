"""Git discovery, path containment, and baseline worktree safety checks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import ProcessResult, require_success, run_process
from ai_dev_loop.state import RunState, sha256_file


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


def git_status_porcelain(repo_root: Path) -> str:
    return require_success(
        _git(["status", "--porcelain=v2", "--untracked-files=all"], cwd=repo_root),
        context="git status",
    )


def git_diff_cached_name_only(repo_root: Path) -> str:
    return require_success(
        _git(["diff", "--cached", "--name-only"], cwd=repo_root),
        context="git diff --cached --name-only",
    )


def git_diff_cached_stat(repo_root: Path) -> str:
    return require_success(
        _git(["diff", "--cached", "--stat"], cwd=repo_root),
        context="git diff --cached --stat",
    )


def git_diff_cached_patch(repo_root: Path) -> str:
    return require_success(
        _git(["diff", "--cached"], cwd=repo_root),
        context="git diff --cached",
    )


def git_add_all(repo_root: Path) -> ProcessResult:
    return _git(["add", "-A"], cwd=repo_root)


def staged_paths_from_name_only(output: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def paths_with_index_changes(status: str) -> set[str]:
    """Return repository-relative paths with staged (index) changes."""
    paths: set[str] = set()
    for line in status.splitlines():
        if not line:
            continue
        if line.startswith("1 "):
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            xy = parts[1]
            if len(xy) >= 1 and xy[0] != ".":
                path = _extract_status_path(line)
                if path:
                    paths.add(path)
            continue
        if line.startswith("2 "):
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            xy = parts[1]
            if len(xy) >= 1 and xy[0] != ".":
                path = _extract_status_path(line)
                if path:
                    paths.add(path)
            continue
        if line.startswith("u "):
            path = _extract_status_path(line)
            if path:
                paths.add(path)
    return paths


def paths_with_worktree_changes(status: str) -> set[str]:
    """Return repository-relative paths appearing in porcelain v2 status."""
    paths: set[str] = set()
    for line in status.splitlines():
        path = _extract_status_path(line)
        if path:
            paths.add(path)
    return paths


def validate_stage_mode(stage_mode: str) -> None:
    if stage_mode != "all":
        raise ValidationError(
            f"unsupported stage_mode for Phase 3: {stage_mode!r}; only 'all' is implemented"
        )


def validate_no_preexisting_staged_paths(staged_paths: tuple[str, ...]) -> None:
    if staged_paths:
        joined = ", ".join(sorted(staged_paths))
        raise ValidationError(
            f"pre-existing staged changes detected before orchestrator staging: {joined}"
        )


def validate_plan_hash_unchanged(repo_root: Path, plan_repo_path: str, expected_hash: str) -> None:
    plan_file = repo_root / plan_repo_path
    if not plan_file.is_file():
        raise ValidationError(f"repository plan file missing: {plan_repo_path}")
    if sha256_file(plan_file) != expected_hash:
        raise ValidationError("repository plan file hash does not match prepared state")


def validate_prompt_source_unchanged(
    status: str,
    prompt_repo_path: str,
    *,
    repo_root: Path,
    prompt_source_path: Path,
) -> None:
    if not is_git_tracked(prompt_source_path, repo_root):
        return
    changed_paths = paths_with_worktree_changes(status)
    if prompt_repo_path in changed_paths:
        raise ValidationError(
            f"prompt source file has tracked changes and cannot be staged: {prompt_repo_path}"
        )


def paths_with_unstaged_changes(status: str) -> set[str]:
    """Return repository-relative tracked paths with unstaged worktree changes."""
    paths: set[str] = set()
    for line in status.splitlines():
        if not line:
            continue
        if line.startswith("1 "):
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            xy = parts[1]
            if len(xy) >= 2 and xy[1] not in {".", " "}:
                path = _extract_status_path(line)
                if path:
                    paths.add(path)
            continue
        if line.startswith("2 "):
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            xy = parts[1]
            if len(xy) >= 2 and xy[1] not in {".", " "}:
                path = _extract_status_path(line)
                if path:
                    paths.add(path)
    return paths


def paths_with_untracked(status: str) -> set[str]:
    paths: set[str] = set()
    for line in status.splitlines():
        if line.startswith("?"):
            path = _extract_status_path(line)
            if path:
                paths.add(path)
    return paths


def normalize_patch_text(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def validate_staged_patch_matches_artifact(repo_root: Path, patch_artifact: Path) -> None:
    if not patch_artifact.is_file():
        raise ValidationError(f"recorded staged patch artifact missing: {patch_artifact}")
    recorded = normalize_patch_text(patch_artifact.read_text(encoding="utf-8"))
    current = normalize_patch_text(git_diff_cached_patch(repo_root))
    if current != recorded:
        raise ValidationError(
            "staged index no longer matches the previous orchestrator-recorded staged patch"
        )


def validate_correction_pre_cursor(repo_root: Path, *, patch_artifact: Path) -> None:
    """Strict pre-Cursor correction checkpoint: previous staged patch must still match."""

    validate_staged_patch_matches_artifact(repo_root, patch_artifact)
    status = git_status_porcelain(repo_root)
    unstaged = paths_with_unstaged_changes(status)
    if unstaged:
        joined = ", ".join(sorted(unstaged))
        raise ValidationError(f"unstaged tracked changes detected before correction: {joined}")
    untracked = paths_with_untracked(status)
    if untracked:
        joined = ", ".join(sorted(untracked))
        raise ValidationError(f"untracked files detected before correction: {joined}")


def validate_usage_limit_recovery_correction_pre_cursor(
    state: RunState,
    run_directory: Path,
    *,
    iteration_number: int,
    patch_artifact: Path,
) -> None:
    """Pre-Cursor checkpoint for cursor-recovery correction iterations.

    Requires the previous staged patch artifact to exist as lineage evidence and
    the recorded usage-limit fingerprint to match the current repository state.
    Verified partial tracked, untracked, and index mutations are allowed.
    """

    from ai_dev_loop.runners.cursor_output import (
        fingerprints_match,
        load_usage_limit_failure_fingerprint,
        recompute_cursor_output_fingerprint,
    )

    if not patch_artifact.is_file():
        raise ValidationError(
            f"previous staged patch lineage artifact missing: {patch_artifact}"
        )

    recorded = load_usage_limit_failure_fingerprint(run_directory, iteration_number)
    if recorded is None:
        raise ValidationError(
            "usage-limit fingerprint missing for recovery correction preflight"
        )

    recovery = state.recovery
    if recovery and recovery.usage_limit_fingerprint_sha256:
        recorded_hash = recorded.get("aggregate_sha256")
        if recorded_hash != recovery.usage_limit_fingerprint_sha256:
            raise ValidationError(
                "usage-limit fingerprint does not match recovery lineage"
            )

    try:
        current = recompute_cursor_output_fingerprint(state, iteration_number=iteration_number)
    except ValidationError:
        raise
    if not fingerprints_match(recorded, current):
        raise ValidationError(
            "repository content drifted since the recorded usage-limit failure"
        )


def validate_repository_identity(
    repo_root: Path,
    *,
    expected_root: str,
    expected_git_common_dir: str,
    expected_git_dir: str,
    expected_branch: str,
    expected_head: str,
    context: str,
) -> None:
    """Require HEAD, branch, and repository identity to match the prepared run."""

    repo_info = discover_repository(repo_root)
    if repo_info.root.resolve() != Path(expected_root).resolve():
        raise ValidationError(f"repository root changed {context}")
    if repo_info.git_common_dir.resolve() != Path(expected_git_common_dir).resolve():
        raise ValidationError(f"git common directory changed {context}")
    if repo_info.git_dir.resolve() != Path(expected_git_dir).resolve():
        raise ValidationError(f"git directory changed {context}")
    if repo_info.branch != expected_branch:
        raise ValidationError(
            f"repository branch changed {context}: expected {expected_branch}, found {repo_info.branch}"
        )
    if repo_info.head != expected_head:
        raise ValidationError(f"repository HEAD changed {context}")


def validate_clean_after_stage_all(repo_root: Path) -> None:
    """After `git add -A`, require no tracked unstaged or untracked non-ignored paths."""

    status = git_status_porcelain(repo_root)
    unstaged = paths_with_unstaged_changes(status)
    if unstaged:
        joined = ", ".join(sorted(unstaged))
        raise ValidationError(f"tracked unstaged changes remain after git add -A: {joined}")
    untracked = paths_with_untracked(status)
    if untracked:
        joined = ", ".join(sorted(untracked))
        raise ValidationError(f"untracked files remain after git add -A: {joined}")


def validate_staged_paths_safe(
    staged_paths: tuple[str, ...],
    *,
    plan_repo_path: str,
    prompt_repo_path: str,
    plan_hash: str,
    repo_root: Path,
) -> None:
    if not staged_paths:
        raise ValidationError("no staged changes after git add -A")

    if prompt_repo_path in staged_paths:
        raise ValidationError(f"prompt source file must not be staged: {prompt_repo_path}")

    validate_plan_hash_unchanged(repo_root, plan_repo_path, plan_hash)

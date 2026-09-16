"""Git discovery, path containment, and baseline worktree safety checks."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.process import ProcessResult, require_success, run_process, run_process_bytes
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


CHECKPOINT_GIT_TIMEOUT_SECONDS = 120.0
CHECKPOINT_GIT_LEASE_BUFFER_SECONDS = 1.0
_EMPTY_HOOKS_DIR = Path(__file__).resolve().parent / "empty_hooks"


@dataclass(frozen=True)
class CheckpointGitDeadline:
    """Absolute tick-lease deadline for checkpoint Git subprocess budgeting."""

    lease_expires_at: datetime | None

    @classmethod
    def from_lease(cls, lease_expires_at: datetime | None) -> CheckpointGitDeadline:
        return cls(lease_expires_at=lease_expires_at)

    def remaining_timeout(self, now: datetime) -> float:
        return checkpoint_git_timeout_for_lease(
            lease_expires_at=self.lease_expires_at,
            now=now,
        )


def _git(args: list[str], *, cwd: Path) -> ProcessResult:
    return run_process(["git", *args], cwd=str(cwd))


def checkpoint_git_config_args() -> list[str]:
    """Command-local Git configuration that suppresses hooks and signing."""

    hooks_path = str(_EMPTY_HOOKS_DIR)
    return [
        "-c",
        f"core.hooksPath={hooks_path}",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "tag.gpgsign=false",
    ]


def checkpoint_git_env(identity: GitIdentity | None = None) -> dict[str, str]:
    """Minimal environment for bounded checkpoint Git subprocesses."""

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if identity is not None:
        env.update(
            {
                "GIT_AUTHOR_NAME": identity.author_name,
                "GIT_AUTHOR_EMAIL": identity.author_email,
                "GIT_AUTHOR_DATE": identity.author_date,
                "GIT_COMMITTER_NAME": identity.committer_name,
                "GIT_COMMITTER_EMAIL": identity.committer_email,
                "GIT_COMMITTER_DATE": identity.committer_date,
            }
        )
    return env


def checkpoint_git_timeout_for_lease(
    *,
    lease_expires_at: datetime | None,
    now: datetime,
) -> float:
    """Bound checkpoint subprocess duration to the remaining tick lease."""

    if lease_expires_at is None:
        return CHECKPOINT_GIT_TIMEOUT_SECONDS
    remaining = (lease_expires_at - now).total_seconds() - CHECKPOINT_GIT_LEASE_BUFFER_SECONDS
    if remaining <= 0:
        raise ValidationError("checkpoint tick lease expired before Git subprocess")
    return min(CHECKPOINT_GIT_TIMEOUT_SECONDS, remaining)


def _checkpoint_git(
    args: list[str],
    *,
    cwd: Path,
    identity: GitIdentity | None = None,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> ProcessResult:
    return run_process(
        ["git", *checkpoint_git_config_args(), *args],
        cwd=str(cwd),
        env=checkpoint_git_env(identity),
        timeout=timeout,
    )


def _checkpoint_git_bytes(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> ProcessResult:
    from ai_dev_loop.process import BinaryProcessResult

    binary: BinaryProcessResult = run_process_bytes(
        ["git", *checkpoint_git_config_args(), *args],
        cwd=str(cwd),
        env=checkpoint_git_env(),
        timeout=timeout,
    )
    return ProcessResult(
        args=list(binary.args),
        returncode=binary.returncode,
        stdout=binary.stdout.decode("utf-8", errors="surrogateescape"),
        stderr=binary.stderr.decode("utf-8", errors="replace"),
        timed_out=binary.timed_out,
    )


def _checkpoint_git_success(
    args: list[str],
    *,
    cwd: Path,
    context: str,
    identity: GitIdentity | None = None,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    result = _checkpoint_git(args, cwd=cwd, identity=identity, timeout=timeout)
    if result.timed_out:
        raise ValidationError(f"{context} timed out during checkpoint")
    return require_success(result, context=context)


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


def git_diff_cached_patch_bytes(repo_root: Path) -> bytes:
    """Exact staged patch bytes for publication/commit identity checks.

    Must match ``GitWriteTransport.read_staged_patch_bytes`` (``git diff --cached
    --binary`` with no stdout stripping). Text-mode ``git_diff_cached_patch`` strips
    trailing whitespace via ``require_success`` and is not suitable for content-bound
    commit verification.
    """

    from ai_dev_loop.process import run_process_bytes

    result = run_process_bytes(["git", "diff", "--cached", "--binary"], cwd=str(repo_root))
    if result.timed_out:
        raise ValidationError("git diff --cached --binary timed out")
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip() or "unknown error"
        raise ValidationError(f"git diff --cached --binary failed: {detail}")
    return result.stdout


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


def validate_staged_patch_matches_artifact(repo_root: Path, patch_artifact: Path) -> None:
    """Verify the live index against the exact bytes persisted after staging.

    Staging persists git diff --cached --binary bytes. Review preflight must
    compare that same canonical representation; text-mode output is lossy for
    binary patches and may be normalized by process helpers.
    """

    if not patch_artifact.is_file():
        raise ValidationError(f"recorded staged patch artifact missing: {patch_artifact}")
    recorded = patch_artifact.read_bytes()
    current = git_diff_cached_patch_bytes(repo_root)
    if current != recorded:
        recorded_sha256 = hashlib.sha256(recorded).hexdigest()
        current_sha256 = hashlib.sha256(current).hexdigest()
        raise ValidationError(
            "staged index no longer matches the previous orchestrator-recorded staged patch "
            f"(recorded_sha256={recorded_sha256}, current_sha256={current_sha256})"
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


def validate_usage_limit_recovery_pre_cursor(
    state: RunState,
    run_directory: Path,
    *,
    iteration_number: int,
) -> None:
    """Pre-Cursor checkpoint for any cursor usage-limit recovery successor.

    Validates that the recorded usage-limit fingerprint (failure-time or
    adoption-time) still matches the current repository content. Applies to
    initial iteration and correction iterations alike. Verified partial
    tracked, untracked, and index mutations captured in the fingerprint are
    allowed.
    """

    from ai_dev_loop.runners.cursor_output import (
        extract_usage_limit_content_fingerprint,
        fingerprints_match,
        load_usage_limit_fingerprint_from_path,
        recompute_cursor_output_fingerprint,
        usage_limit_failure_fingerprint_rel_path,
    )

    recovery = state.recovery
    fingerprint_rel: str | None = None
    if recovery and recovery.usage_limit_fingerprint_path:
        fingerprint_rel = recovery.usage_limit_fingerprint_path
    if fingerprint_rel is None:
        fingerprint_rel = usage_limit_failure_fingerprint_rel_path(iteration_number)

    recorded = load_usage_limit_fingerprint_from_path(run_directory, fingerprint_rel)
    if recorded is None:
        raise ValidationError("usage-limit fingerprint missing for recovery preflight")

    content_fingerprint = extract_usage_limit_content_fingerprint(recorded)
    if content_fingerprint is None:
        raise ValidationError(
            "usage-limit fingerprint artifact is missing embedded content evidence"
        )

    if recovery and recovery.usage_limit_fingerprint_sha256:
        recorded_hash = recorded.get("aggregate_sha256")
        if recorded_hash != recovery.usage_limit_fingerprint_sha256:
            raise ValidationError("usage-limit fingerprint does not match recovery lineage")

    try:
        current = recompute_cursor_output_fingerprint(state, iteration_number=iteration_number)
    except ValidationError:
        raise
    if not fingerprints_match(content_fingerprint, current):
        raise ValidationError("repository content drifted since the recorded usage-limit failure")


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

    if not patch_artifact.is_file():
        raise ValidationError(f"previous staged patch lineage artifact missing: {patch_artifact}")
    validate_usage_limit_recovery_pre_cursor(
        state,
        run_directory,
        iteration_number=iteration_number,
    )


def validate_repository_layout(
    repo_root: Path,
    *,
    expected_root: str,
    expected_git_common_dir: str,
    expected_git_dir: str,
    expected_branch: str,
    context: str,
) -> None:
    """Require branch and repository identity without comparing HEAD."""

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

    validate_repository_layout(
        repo_root,
        expected_root=expected_root,
        expected_git_common_dir=expected_git_common_dir,
        expected_git_dir=expected_git_dir,
        expected_branch=expected_branch,
        context=context,
    )
    repo_info = discover_repository(repo_root)
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


@dataclass(frozen=True)
class GitIdentity:
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str


def git_timestamp_from_intent_date(intent_date: str) -> str:
    """Normalize a frozen ISO intent timestamp to git commit-object form."""

    from datetime import UTC, datetime

    stripped = intent_date.strip()
    if not stripped:
        raise ValidationError("intent timestamp is empty")
    if "T" not in stripped and stripped[0].isdigit() and " " in stripped:
        return stripped
    normalized = stripped.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    unix_ts = int(parsed.timestamp())
    offset = parsed.strftime("%z") or "+0000"
    return f"{unix_ts} {offset}"


def _parse_git_ident_line(value: str) -> tuple[str, str, str]:
    if " <" not in value or ">" not in value:
        raise ValidationError("unable to parse git identity line")
    name, remainder = value.split(" <", 1)
    email, timestamp = remainder.split(">", 1)
    name = name.strip()
    email = email.strip()
    timestamp = timestamp.strip()
    if not name or not email or not timestamp:
        raise ValidationError("git identity is incomplete")
    return name, email, timestamp


def resolve_git_identity(repo_root: Path) -> GitIdentity:
    """Resolve author/committer identity without mutating repository configuration."""

    author_ident = require_success(
        _git(["var", "GIT_AUTHOR_IDENT"], cwd=repo_root),
        context="git var GIT_AUTHOR_IDENT",
    )
    author_name, author_email, author_date = _parse_git_ident_line(author_ident)
    committer_ident = require_success(
        _git(["var", "GIT_COMMITTER_IDENT"], cwd=repo_root),
        context="git var GIT_COMMITTER_IDENT",
    )
    committer_name, committer_email, committer_date = _parse_git_ident_line(committer_ident)
    return GitIdentity(
        author_name=author_name,
        author_email=author_email,
        author_date=author_date,
        committer_name=committer_name,
        committer_email=committer_email,
        committer_date=committer_date,
    )


def git_write_tree(repo_root: Path) -> str:
    return checkpoint_git_write_tree(repo_root)


def checkpoint_git_write_tree(
    repo_root: Path,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    return _checkpoint_git_success(
        ["write-tree"],
        cwd=repo_root,
        context="git write-tree",
        timeout=timeout,
    ).strip()


def compute_ephemeral_staged_tree_sha(
    repo_root: Path,
    *,
    parent_head: str,
    patch_bytes: bytes,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Compute staged tree SHA without mutating the repository index or refs."""

    import shutil
    import tempfile

    alternate_objects = _checkpoint_git_success(
        ["rev-parse", "--git-path", "objects"],
        cwd=repo_root,
        context="ephemeral git-path objects",
        timeout=timeout,
    ).strip()

    def ephemeral_git(
        args: list[str],
        *,
        context: str,
        object_dir: Path,
    ) -> str:
        env = checkpoint_git_env()
        env["GIT_INDEX_FILE"] = str(index_path)
        env["GIT_OBJECT_DIRECTORY"] = str(object_dir)
        env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = alternate_objects
        result = run_process(
            ["git", *checkpoint_git_config_args(), *args],
            cwd=str(repo_root),
            env=env,
            timeout=timeout,
        )
        if result.timed_out:
            raise ValidationError(f"{context} timed out during ephemeral tree computation")
        return require_success(result, context=context)

    with tempfile.NamedTemporaryFile(delete=False) as handle:
        index_path = Path(handle.name)
    object_dir = Path(tempfile.mkdtemp(prefix="ai-dev-loop-ephemeral-objects-"))
    patch_path: Path | None = None
    try:
        ephemeral_git(
            ["read-tree", parent_head],
            context="ephemeral read-tree",
            object_dir=object_dir,
        )
        with tempfile.NamedTemporaryFile(
            prefix=".ai-dev-loop-ephemeral-patch-",
            suffix=".patch",
            delete=False,
        ) as patch_handle:
            patch_path = Path(patch_handle.name)
            patch_handle.write(patch_bytes)
        ephemeral_git(
            ["apply", "--whitespace=nowarn", "--cached", str(patch_path)],
            context="ephemeral apply --cached",
            object_dir=object_dir,
        )
        return ephemeral_git(
            ["write-tree"],
            context="ephemeral write-tree",
            object_dir=object_dir,
        ).strip()
    finally:
        index_path.unlink(missing_ok=True)
        if patch_path is not None:
            patch_path.unlink(missing_ok=True)
        shutil.rmtree(object_dir, ignore_errors=True)


def git_rev_parse(repo_root: Path, ref: str) -> str:
    return checkpoint_git_rev_parse(repo_root, ref)


def checkpoint_git_rev_parse(
    repo_root: Path,
    ref: str,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    return _checkpoint_git_success(
        ["rev-parse", ref],
        cwd=repo_root,
        context=f"git rev-parse {ref}",
        timeout=timeout,
    ).strip()


def git_symbolic_ref(repo_root: Path, ref: str) -> str:
    return checkpoint_git_symbolic_ref(repo_root, ref)


def checkpoint_git_symbolic_ref(
    repo_root: Path,
    ref: str,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    return _checkpoint_git_success(
        ["symbolic-ref", ref],
        cwd=repo_root,
        context=f"git symbolic-ref {ref}",
        timeout=timeout,
    ).strip()


def checkpoint_git_status_porcelain(
    repo_root: Path,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    return _checkpoint_git_success(
        ["status", "--porcelain=v2", "--untracked-files=all"],
        cwd=repo_root,
        context="git status",
        timeout=timeout,
    )


def checkpoint_git_diff_cached_patch_bytes(
    repo_root: Path,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> bytes:
    result = _checkpoint_git_bytes(
        ["diff", "--cached", "--binary"],
        cwd=repo_root,
        timeout=timeout,
    )
    if result.timed_out:
        raise ValidationError("git diff --cached --binary timed out during checkpoint")
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown error"
        raise ValidationError(f"git diff --cached --binary failed: {detail}")
    return result.stdout.encode("utf-8", errors="surrogateescape")


def checkpoint_validate_repository_layout(
    repo_root: Path,
    *,
    expected_root: str,
    expected_git_common_dir: str,
    expected_git_dir: str,
    expected_branch: str,
    context: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    repo_root = repo_root.resolve()
    root = Path(
        _checkpoint_git_success(
            ["rev-parse", "--show-toplevel"],
            cwd=repo_root,
            context="git root",
            timeout=timeout,
        )
    ).resolve()
    git_common_dir = Path(
        _checkpoint_git_success(
            ["rev-parse", "--git-common-dir"],
            cwd=repo_root,
            context="git common dir",
            timeout=timeout,
        )
    )
    if not git_common_dir.is_absolute():
        git_common_dir = (root / git_common_dir).resolve()
    git_dir = Path(
        _checkpoint_git_success(
            ["rev-parse", "--git-dir"],
            cwd=repo_root,
            context="git dir",
            timeout=timeout,
        )
    )
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    branch = _checkpoint_git_success(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        context="git branch",
        timeout=timeout,
    )
    if root.resolve() != Path(expected_root).resolve():
        raise ValidationError(f"repository root changed {context}")
    if git_common_dir.resolve() != Path(expected_git_common_dir).resolve():
        raise ValidationError(f"git common directory changed {context}")
    if git_dir.resolve() != Path(expected_git_dir).resolve():
        raise ValidationError(f"git directory changed {context}")
    if branch != expected_branch:
        raise ValidationError(
            f"repository branch changed {context}: expected {expected_branch}, found {branch}"
        )


def checkpoint_validate_checked_out_branch(
    repo_root: Path,
    *,
    expected_branch: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    branch = _checkpoint_git_success(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        context="git branch",
        timeout=timeout,
    )
    if branch == "HEAD":
        raise ValidationError("repository is on detached HEAD")
    if branch != expected_branch:
        raise ValidationError(
            f"checked-out branch drift: expected {expected_branch}, found {branch}"
        )
    return branch


def checkpoint_resolve_git_identity(
    repo_root: Path,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> GitIdentity:
    author_ident = _checkpoint_git_success(
        ["var", "GIT_AUTHOR_IDENT"],
        cwd=repo_root,
        context="git var GIT_AUTHOR_IDENT",
        timeout=timeout,
    )
    author_name, author_email, author_date = _parse_git_ident_line(author_ident)
    committer_ident = _checkpoint_git_success(
        ["var", "GIT_COMMITTER_IDENT"],
        cwd=repo_root,
        context="git var GIT_COMMITTER_IDENT",
        timeout=timeout,
    )
    committer_name, committer_email, committer_date = _parse_git_ident_line(committer_ident)
    return GitIdentity(
        author_name=author_name,
        author_email=author_email,
        author_date=author_date,
        committer_name=committer_name,
        committer_email=committer_email,
        committer_date=committer_date,
    )


def checkpoint_validate_staged_patch_matches_artifact(
    repo_root: Path,
    patch_artifact: Path,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    if not patch_artifact.is_file():
        raise ValidationError(f"recorded staged patch artifact missing: {patch_artifact}")
    recorded = patch_artifact.read_bytes()
    current = checkpoint_git_diff_cached_patch_bytes(repo_root, timeout=timeout)
    if current != recorded:
        recorded_sha256 = hashlib.sha256(recorded).hexdigest()
        current_sha256 = hashlib.sha256(current).hexdigest()
        raise ValidationError(
            "staged index no longer matches the previous orchestrator-recorded staged patch "
            f"(recorded_sha256={recorded_sha256}, current_sha256={current_sha256})"
        )


def git_commit_tree(
    repo_root: Path,
    *,
    tree_sha: str,
    parent_sha: str,
    message: str,
    identity: GitIdentity,
) -> str:
    """Create an unsigned commit object without invoking hooks or porcelain commit."""

    return checkpoint_git_commit_tree(
        repo_root,
        tree_sha=tree_sha,
        parent_sha=parent_sha,
        message=message,
        identity=identity,
    )


def checkpoint_git_commit_tree(
    repo_root: Path,
    *,
    tree_sha: str,
    parent_sha: str,
    message: str,
    identity: GitIdentity,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str:
    result = _checkpoint_git(
        [
            "commit-tree",
            tree_sha,
            "-p",
            parent_sha,
            "-m",
            message,
        ],
        cwd=repo_root,
        identity=identity,
        timeout=timeout,
    )
    if result.timed_out:
        raise ValidationError("git commit-tree timed out during checkpoint")
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown error"
        raise ValidationError(f"git commit-tree failed: {detail}")
    commit_sha = result.stdout.strip()
    if not commit_sha:
        raise ValidationError("git commit-tree returned empty commit SHA")
    return commit_sha


def git_update_ref_cas(
    repo_root: Path,
    *,
    ref: str,
    new_sha: str,
    old_sha: str,
) -> None:
    """Compare-and-swap a local branch ref without checkout/reset."""

    checkpoint_git_update_ref_cas(
        repo_root,
        ref=ref,
        new_sha=new_sha,
        old_sha=old_sha,
    )


def checkpoint_git_update_ref_cas(
    repo_root: Path,
    *,
    ref: str,
    new_sha: str,
    old_sha: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    result = _checkpoint_git(
        ["update-ref", ref, new_sha, old_sha],
        cwd=repo_root,
        timeout=timeout,
    )
    if result.timed_out:
        raise ValidationError("git update-ref timed out during checkpoint")
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown error"
        raise ValidationError(f"git update-ref CAS failed: {detail}")


def validate_checked_out_branch(repo_root: Path, *, expected_branch: str) -> str:
    branch = require_success(
        _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root),
        context="git branch",
    )
    if branch == "HEAD":
        raise ValidationError("repository is on detached HEAD")
    if branch != expected_branch:
        raise ValidationError(
            f"checked-out branch drift: expected {expected_branch}, found {branch}"
        )
    return branch


def validate_checkpoint_postconditions(
    repo_root: Path,
    *,
    expected_tree: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Require HEAD tree match, empty index, and clean worktree after checkpoint."""

    head_tree = checkpoint_git_rev_parse(repo_root, "HEAD^{tree}", timeout=timeout)
    if head_tree != expected_tree:
        raise ValidationError("HEAD tree does not match reviewed checkpoint tree")
    staged_patch = checkpoint_git_diff_cached_patch_bytes(repo_root, timeout=timeout)
    if staged_patch:
        raise ValidationError("staged index must be empty after checkpoint")
    status = checkpoint_git_status_porcelain(repo_root, timeout=timeout)
    unstaged = paths_with_unstaged_changes(status)
    if unstaged:
        joined = ", ".join(sorted(unstaged))
        raise ValidationError(f"tracked unstaged changes remain after checkpoint: {joined}")
    untracked = paths_with_untracked(status)
    if untracked:
        joined = ", ".join(sorted(untracked))
        raise ValidationError(f"untracked files remain after checkpoint: {joined}")


def validate_post_checkpoint_clean(repo_root: Path, *, expected_tree: str) -> None:
    validate_checkpoint_postconditions(repo_root, expected_tree=expected_tree)


def checkpoint_verify_commit_identity(
    repo_root: Path,
    *,
    commit_sha: str,
    tree_sha: str,
    parent_sha: str,
    message: str,
    identity: GitIdentity,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Verify an existing commit object matches the frozen checkpoint intent."""

    commit_tree = checkpoint_git_rev_parse(repo_root, f"{commit_sha}^{{tree}}", timeout=timeout)
    if commit_tree != tree_sha:
        raise ValidationError("commit object tree does not match reviewed tree")
    commit_parent = checkpoint_git_rev_parse(repo_root, f"{commit_sha}^", timeout=timeout)
    if commit_parent != parent_sha:
        raise ValidationError("commit object parent does not match frozen parent HEAD")
    raw_commit = _checkpoint_git_success(
        ["cat-file", "-p", commit_sha],
        cwd=repo_root,
        context=f"git cat-file -p {commit_sha}",
        timeout=timeout,
    )
    lines = raw_commit.splitlines()
    if len(lines) < 4:
        raise ValidationError("commit object payload is truncated")
    if lines[0] != f"tree {tree_sha}":
        raise ValidationError("commit object tree line mismatch")
    if lines[1] != f"parent {parent_sha}":
        raise ValidationError("commit object parent line mismatch")
    author_line = lines[2]
    committer_line = lines[3]
    if not author_line.startswith("author "):
        raise ValidationError("commit object author line missing")
    if not committer_line.startswith("committer "):
        raise ValidationError("commit object committer line missing")
    expected_author = (
        f"author {identity.author_name} <{identity.author_email}> {identity.author_date}"
    )
    expected_committer = (
        f"committer {identity.committer_name} <{identity.committer_email}> "
        f"{identity.committer_date}"
    )
    if author_line != expected_author:
        raise ValidationError("commit object author identity mismatch")
    if committer_line != expected_committer:
        raise ValidationError("commit object committer identity mismatch")
    if len(lines) < 5 or lines[4] != "":
        raise ValidationError("commit object message separator missing")
    commit_message = "\n".join(lines[5:])
    if commit_message != message:
        raise ValidationError("commit object message does not match checkpoint intent")


def recovery_git_private_ref_peek(
    repo_root: Path,
    *,
    ref: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> str | None:
    """Return the ref SHA when present, None when definitively absent, raise on probe failure."""

    if not ref.startswith("refs/ai-dev-loop/recovery/"):
        raise ValidationError("recovery private ref must use package namespace")
    result = run_process(
        ["git", *checkpoint_git_config_args(), "rev-parse", "--verify", ref],
        cwd=str(repo_root),
        env=checkpoint_git_env(),
        timeout=timeout,
    )
    if result.timed_out:
        raise ValidationError("git rev-parse timed out while probing recovery private ref")
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode in {1, 128}:
        return None
    raise ValidationError("git rev-parse failed while probing recovery private ref")


def recovery_git_private_ref_exists(
    repo_root: Path,
    *,
    ref: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> bool:
    return recovery_git_private_ref_peek(repo_root, ref=ref, timeout=timeout) is not None


def recovery_git_create_private_ref(
    repo_root: Path,
    *,
    ref: str,
    parent_head: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Create a package-owned private recovery ref at an exact parent commit."""

    if not ref.startswith("refs/ai-dev-loop/recovery/"):
        raise ValidationError("recovery private ref must use package namespace")
    existing = recovery_git_private_ref_peek(repo_root, ref=ref, timeout=timeout)
    if existing == parent_head:
        return
    if existing is not None:
        raise ValidationError("recovery private ref collision at different commit")
    null_sha = "0" * 40
    try:
        checkpoint_git_update_ref_cas(
            repo_root,
            ref=ref,
            new_sha=parent_head,
            old_sha=null_sha,
            timeout=timeout,
        )
    except ValidationError:
        current = recovery_git_private_ref_peek(repo_root, ref=ref, timeout=timeout)
        if current != parent_head:
            raise ValidationError("recovery private ref collision during atomic create") from None


def recovery_git_update_private_ref_cas(
    repo_root: Path,
    *,
    ref: str,
    new_sha: str,
    old_sha: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    if not ref.startswith("refs/ai-dev-loop/recovery/"):
        raise ValidationError("recovery private ref must use package namespace")
    checkpoint_git_update_ref_cas(
        repo_root,
        ref=ref,
        new_sha=new_sha,
        old_sha=old_sha,
        timeout=timeout,
    )


def recovery_git_checkout_detach(
    repo_root: Path,
    *,
    commit_sha: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Move a recovery worktree HEAD to an exact commit without branch mutation."""

    _checkpoint_git_success(
        ["checkout", "--detach", commit_sha],
        cwd=repo_root,
        context="recovery checkout detach",
        timeout=timeout,
    )


def recovery_git_worktree_add(
    repo_root: Path,
    *,
    worktree_path: Path,
    parent_head: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Create a detached package-managed worktree at an exact parent commit."""

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _checkpoint_git_success(
        [
            "worktree",
            "add",
            "--detach",
            str(worktree_path),
            parent_head,
        ],
        cwd=repo_root,
        context="recovery worktree add",
        timeout=timeout,
    )


def recovery_git_apply_staged_patch(
    repo_root: Path,
    *,
    patch_bytes: bytes,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Apply an authenticated staged patch to index and worktree without shell interpolation."""

    import hashlib
    import tempfile

    expected_patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    live_cached = checkpoint_git_diff_cached_patch_bytes(repo_root)
    live_cached_sha = hashlib.sha256(live_cached).hexdigest()

    with tempfile.NamedTemporaryFile(
        prefix=".ai-dev-loop-recovery-patch-",
        suffix=".patch",
        dir=repo_root,
        delete=False,
    ) as handle:
        patch_path = Path(handle.name)
        handle.write(patch_bytes)
    try:
        if live_cached_sha != expected_patch_sha:
            _checkpoint_git_success(
                ["apply", "--whitespace=nowarn", "--cached", str(patch_path)],
                cwd=repo_root,
                context="recovery git apply",
                timeout=timeout,
            )
        check = _checkpoint_git(
            ["apply", "--whitespace=nowarn", "--check", str(patch_path)],
            cwd=repo_root,
            timeout=timeout,
        )
        if check.returncode == 0:
            _checkpoint_git_success(
                ["apply", "--whitespace=nowarn", str(patch_path)],
                cwd=repo_root,
                context="recovery git apply",
                timeout=timeout,
            )
    finally:
        patch_path.unlink(missing_ok=True)


def recovery_git_apply_worktree_patch(
    repo_root: Path,
    *,
    patch_bytes: bytes,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Apply a patch to the worktree only without mutating the index."""

    import tempfile

    with tempfile.NamedTemporaryFile(
        prefix=".ai-dev-loop-recovery-patch-",
        suffix=".patch",
        dir=repo_root,
        delete=False,
    ) as handle:
        patch_path = Path(handle.name)
        handle.write(patch_bytes)
    try:
        check = _checkpoint_git(
            ["apply", "--whitespace=nowarn", "--check", str(patch_path)],
            cwd=repo_root,
            timeout=timeout,
        )
        if check.returncode != 0:
            _checkpoint_git_success(
                ["apply", "--whitespace=nowarn", "--check", str(patch_path)],
                cwd=repo_root,
                context="recovery git apply worktree check",
                timeout=timeout,
            )
        _checkpoint_git_success(
            ["apply", "--whitespace=nowarn", str(patch_path)],
            cwd=repo_root,
            context="recovery git apply worktree",
            timeout=timeout,
        )
    finally:
        patch_path.unlink(missing_ok=True)


def recovery_git_worktree_contains(
    repo_root: Path,
    *,
    worktree_path: Path,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> bool:
    """Return True when the exact worktree path is registered under the target repository."""

    result = _checkpoint_git(
        ["worktree", "list", "--porcelain"],
        cwd=repo_root,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ValidationError("git worktree list failed during recovery ownership check")
    normalized = str(worktree_path.resolve())
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            listed = line.removeprefix("worktree ").strip()
            if str(Path(listed).resolve()) == normalized:
                return True
    return False


def recovery_git_worktree_remove(
    repo_root: Path,
    *,
    worktree_path: Path,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Remove an exact owned recovery worktree without --force."""

    _checkpoint_git_success(
        ["worktree", "remove", str(worktree_path)],
        cwd=repo_root,
        context="recovery worktree remove",
        timeout=timeout,
    )


def recovery_git_delete_ref(
    repo_root: Path,
    *,
    ref: str,
    expected_sha: str,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> None:
    """Delete a private recovery ref with an atomic expected-old-value update."""

    if not ref.startswith("refs/ai-dev-loop/recovery/"):
        raise ValidationError("recovery private ref must use package namespace")
    result = _checkpoint_git(
        ["update-ref", "-d", ref, expected_sha],
        cwd=repo_root,
        timeout=timeout,
    )
    if result.timed_out:
        raise ValidationError("git update-ref timed out during private ref delete")
    if result.returncode != 0:
        current = recovery_git_private_ref_peek(repo_root, ref=ref, timeout=timeout)
        if current is None:
            return
        if current != expected_sha:
            raise ValidationError("private recovery ref SHA drift before deletion")
        raise ValidationError(
            f"git update-ref private ref delete failed: {result.stderr.strip() or 'unknown error'}"
        )

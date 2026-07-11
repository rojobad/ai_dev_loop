"""Post-Cursor and post-normalization content fingerprints for staging recovery."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import iteration_label
from ai_dev_loop.runners.git import (
    discover_repository,
    git_status_porcelain,
    paths_with_untracked,
    paths_with_worktree_changes,
)
from ai_dev_loop.state import RunState, atomic_write_json, sha256_bytes, utc_now


def cursor_output_fingerprint_rel_path(iteration_number: int) -> str:
    return f"git/cursor-output/{iteration_label(iteration_number)}.json"


def staging_normalization_fingerprint_rel_path(iteration_number: int) -> str:
    return f"git/cursor-output/{iteration_label(iteration_number)}.post-normalization.json"


def normalize_status_text(status: str) -> str:
    return status.replace("\r\n", "\n").rstrip("\n")


def _run_git_bytes(args: list[str], *, cwd: Path) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ValidationError("git executable not found") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip() or "git command failed"
        raise ValidationError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _hash_bytes_stream(data: bytes) -> str:
    return sha256_bytes(data)


def _hash_file_streaming(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_special_path(repo_root: Path, relative: str, *, context: str) -> None:
    path = repo_root / relative
    try:
        st = path.lstat()
    except OSError as exc:
        raise ValidationError(f"unable to inspect path for {context}: {relative}") from exc
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise ValidationError(f"unsupported symlink in {context}: {relative}")
    if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        raise ValidationError(f"unsupported special file in {context}: {relative}")
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValidationError(f"unsupported file type in {context}: {relative}")


@dataclass(frozen=True)
class CursorOutputFingerprint:
    relative_path: str
    payload: dict[str, Any]
    aggregate_sha256: str


def _compute_fingerprint_payload(
    state: RunState,
    *,
    iteration_number: int,
    context: str,
    status_text: str | None = None,
    kind: str,
) -> dict[str, Any]:
    repo_root = Path(state.repository.root)
    repo_info = discover_repository(repo_root)
    if repo_info.root.resolve() != repo_root.resolve():
        raise ValidationError(f"repository root changed {context}")
    if repo_info.git_common_dir.resolve() != Path(state.repository.git_common_dir).resolve():
        raise ValidationError(f"git common directory changed {context}")
    if repo_info.git_dir.resolve() != Path(state.repository.git_dir).resolve():
        raise ValidationError(f"git directory changed {context}")
    if repo_info.branch != state.repository.branch:
        raise ValidationError(
            f"repository branch changed {context}: expected {state.repository.branch}, "
            f"found {repo_info.branch}"
        )
    if repo_info.head != state.repository.initial_head:
        raise ValidationError(f"repository HEAD changed {context}")

    status = normalize_status_text(
        status_text if status_text is not None else repo_info.status_porcelain
    )
    for relative in sorted(paths_with_worktree_changes(status)):
        candidate = repo_root / relative
        if candidate.exists() or candidate.is_symlink():
            _reject_special_path(repo_root, relative, context=context)

    status_sha256 = _hash_bytes_stream(status.encode("utf-8"))
    cached_diff = _run_git_bytes(["diff", "--cached", "--binary"], cwd=repo_root)
    cached_diff_sha256 = _hash_bytes_stream(cached_diff)
    worktree_diff = _run_git_bytes(["diff", "--binary"], cwd=repo_root)
    worktree_diff_sha256 = _hash_bytes_stream(worktree_diff)

    untracked = sorted(paths_with_untracked(status))
    untracked_entries: list[dict[str, str]] = []
    for relative in untracked:
        path = repo_root / relative
        if not path.is_file():
            raise ValidationError(f"untracked path is not a regular file: {relative}")
        _reject_special_path(repo_root, relative, context=context)
        untracked_entries.append({"path": relative, "sha256": _hash_file_streaming(path)})

    aggregate_material = "\n".join(
        [
            f"status:{status_sha256}",
            f"cached:{cached_diff_sha256}",
            f"worktree:{worktree_diff_sha256}",
            "untracked:",
            *[f"{entry['path']}:{entry['sha256']}" for entry in untracked_entries],
            f"branch:{repo_info.branch}",
            f"head:{repo_info.head}",
        ]
    )
    aggregate_sha256 = _hash_bytes_stream(aggregate_material.encode("utf-8"))
    return {
        "schema_version": 1,
        "kind": kind,
        "iteration": iteration_number,
        "captured_at": utc_now().isoformat(),
        "branch": repo_info.branch,
        "head": repo_info.head,
        "repository_root_matches_state": True,
        "status_porcelain_sha256": status_sha256,
        "cached_diff_sha256": cached_diff_sha256,
        "worktree_diff_sha256": worktree_diff_sha256,
        "untracked_files": untracked_entries,
        "aggregate_sha256": aggregate_sha256,
    }


def capture_cursor_output_fingerprint(
    state: RunState,
    run_directory: Path,
    *,
    iteration_number: int,
    status_text: str | None = None,
) -> CursorOutputFingerprint:
    """Capture deterministic Git/content evidence after Cursor and before staging."""

    payload = _compute_fingerprint_payload(
        state,
        iteration_number=iteration_number,
        context="after Cursor",
        status_text=status_text,
        kind="post_cursor",
    )
    relative_path = cursor_output_fingerprint_rel_path(iteration_number)
    atomic_write_json(run_directory / relative_path, payload, sensitive=True)
    return CursorOutputFingerprint(
        relative_path=relative_path,
        payload=payload,
        aggregate_sha256=str(payload["aggregate_sha256"]),
    )


def capture_staging_normalization_fingerprint(
    state: RunState,
    run_directory: Path,
    *,
    iteration_number: int,
) -> CursorOutputFingerprint:
    """Capture fingerprint immediately after successful `git add -A` normalization."""

    payload = _compute_fingerprint_payload(
        state,
        iteration_number=iteration_number,
        context="after staging normalization",
        kind="post_normalization",
    )
    relative_path = staging_normalization_fingerprint_rel_path(iteration_number)
    atomic_write_json(run_directory / relative_path, payload, sensitive=True)
    return CursorOutputFingerprint(
        relative_path=relative_path,
        payload=payload,
        aggregate_sha256=str(payload["aggregate_sha256"]),
    )


def load_fingerprint_artifact(
    run_directory: Path,
    relative_path: str,
) -> dict[str, Any] | None:
    path = run_directory / relative_path
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_cursor_output_fingerprint(
    run_directory: Path,
    iteration_number: int,
) -> dict[str, Any] | None:
    return load_fingerprint_artifact(
        run_directory,
        cursor_output_fingerprint_rel_path(iteration_number),
    )


def load_staging_normalization_fingerprint(
    run_directory: Path,
    iteration_number: int,
) -> dict[str, Any] | None:
    return load_fingerprint_artifact(
        run_directory,
        staging_normalization_fingerprint_rel_path(iteration_number),
    )


def recompute_cursor_output_fingerprint(
    state: RunState,
    *,
    iteration_number: int,
) -> CursorOutputFingerprint:
    """Recompute fingerprint without writing an artifact (recovery comparison)."""

    payload = _compute_fingerprint_payload(
        state,
        iteration_number=iteration_number,
        context="while recomputing fingerprint",
        kind="recomputed",
    )
    return CursorOutputFingerprint(
        relative_path=cursor_output_fingerprint_rel_path(iteration_number),
        payload=payload,
        aggregate_sha256=str(payload["aggregate_sha256"]),
    )


def fingerprints_match(recorded: dict[str, Any], current: CursorOutputFingerprint) -> bool:
    recorded_hash = recorded.get("aggregate_sha256")
    if not isinstance(recorded_hash, str):
        return False
    return recorded_hash == current.aggregate_sha256


def after_cursor_status_matches(
    run_directory: Path,
    *,
    iteration_number: int,
    repo_root: Path,
) -> bool:
    recorded_path = (
        run_directory / f"git/status/{iteration_label(iteration_number)}-after-cursor.txt"
    )
    if not recorded_path.is_file():
        return False
    recorded = normalize_status_text(recorded_path.read_text(encoding="utf-8"))
    current = normalize_status_text(git_status_porcelain(repo_root))
    return recorded == current

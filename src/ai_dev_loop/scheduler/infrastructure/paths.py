"""XDG path helpers for the central scheduler ledger."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ai_dev_loop.paths import DIR_MODE, ensure_dir, state_dir

DEFAULT_DB_FILENAME = "engine.sqlite3"
ARTIFACTS_DIRNAME = "artifacts"
RUNS_DIRNAME = "runs"
SEQUENCES_DIRNAME = "sequences"

_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def scheduler_state_dir() -> Path:
    return state_dir()


def ensure_scheduler_state_dir(path: Path | None = None) -> Path:
    target = path if path is not None else scheduler_state_dir()
    return ensure_dir(target, mode=DIR_MODE)


def default_engine_db_path() -> Path:
    return scheduler_state_dir() / DEFAULT_DB_FILENAME


def engine_db_path_for_state_dir(state_root: Path) -> Path:
    return state_root / DEFAULT_DB_FILENAME


def default_artifact_root() -> Path:
    return scheduler_state_dir() / ARTIFACTS_DIRNAME


def secure_database_path(db_path: Path, *, create_parent: bool = True) -> Path:
    if create_parent:
        ensure_scheduler_state_dir(db_path.parent)
    return db_path


def apply_database_permissions(db_path: Path) -> None:
    from ai_dev_loop.paths import set_sensitive_file_mode

    set_sensitive_file_mode(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            set_sensitive_file_mode(sidecar)


def _reject_symlink_component(path: Path, *, label: str) -> None:
    if path.exists() and path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")


def _validate_lexical_relative_path(relative_path: str) -> tuple[str, ...]:
    if not relative_path or relative_path.startswith(("/", "\\")):
        raise ValueError("relative_path must be run-relative")
    if "\\" in relative_path:
        raise ValueError("relative_path must use forward slashes")
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must be a lexical safe run-relative path")
    if not _SAFE_RELATIVE_PATH.match(relative_path):
        raise ValueError("relative_path contains unsafe characters")
    return parts


def _reject_symlink_lexical_components(root: Path, parts: tuple[str, ...]) -> None:
    root_resolved = root.resolve(strict=False)
    _reject_symlink_component(root_resolved, label="artifact root")
    current = root_resolved
    for part in parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("artifact path must not traverse a symlink")


def _secure_existing_directory(path: Path, *, label: str) -> None:
    if os.name == "nt":
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        try:
            os.chmod(path, DIR_MODE)
        except OSError as exc:
            raise ValueError(f"{label} has unsafe permissions") from exc
        return
    if mode != DIR_MODE:
        os.chmod(path, DIR_MODE)


def _ensure_private_directory(path: Path, *, label: str) -> Path:
    _reject_symlink_component(path, label=label)
    if not path.exists():
        return ensure_dir(path, mode=DIR_MODE)
    if not path.is_dir():
        raise ValueError(f"{label} must be a directory")
    _secure_existing_directory(path, label=label)
    return path


def ensure_artifact_root(artifact_root: Path) -> Path:
    _reject_symlink_component(artifact_root, label="artifact root")
    artifact_resolved = artifact_root.resolve(strict=False)
    _reject_symlink_component(artifact_resolved, label="artifact root")
    return _ensure_private_directory(artifact_resolved, label="artifact root")


def reject_repository_overlap(artifact_root: Path, repository_root: Path) -> None:
    artifact_resolved = artifact_root.resolve(strict=False)
    repository_resolved = repository_root.resolve(strict=False)
    try:
        artifact_resolved.relative_to(repository_resolved)
    except ValueError:
        pass
    else:
        raise ValueError("artifact root must not be inside the target repository")
    try:
        repository_resolved.relative_to(artifact_resolved)
    except ValueError:
        pass
    else:
        raise ValueError("target repository must not be inside the artifact root")


def safe_run_directory_key(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def run_artifact_root(artifact_root: Path, run_id: str) -> Path:
    return artifact_root / RUNS_DIRNAME / safe_run_directory_key(run_id)


def ensure_run_artifact_root(artifact_root: Path, run_id: str) -> Path:
    artifact_resolved = ensure_artifact_root(artifact_root)
    _ensure_private_directory(
        artifact_resolved / RUNS_DIRNAME,
        label="runs artifact root",
    )
    return _ensure_private_directory(
        run_artifact_root(artifact_resolved, run_id),
        label="run artifact root",
    )


def safe_sequence_directory_key(sequence_id: str) -> str:
    return hashlib.sha256(sequence_id.encode("utf-8")).hexdigest()


def sequence_artifact_root(artifact_root: Path, sequence_id: str) -> Path:
    return artifact_root / SEQUENCES_DIRNAME / safe_sequence_directory_key(sequence_id)


def ensure_sequence_artifact_root(artifact_root: Path, sequence_id: str) -> Path:
    artifact_resolved = ensure_artifact_root(artifact_root)
    _ensure_private_directory(
        artifact_resolved / SEQUENCES_DIRNAME,
        label="sequences artifact root",
    )
    return _ensure_private_directory(
        sequence_artifact_root(artifact_resolved, sequence_id),
        label="sequence artifact root",
    )


def resolve_sequence_relative_path(sequence_root: Path, relative_path: str) -> Path:
    return resolve_run_relative_path(sequence_root, relative_path)


def ensure_artifact_parent_directories(root: Path, relative_path: str) -> None:
    parts = _validate_lexical_relative_path(relative_path)
    if len(parts) < 2:
        return
    root_resolved = root.resolve(strict=False)
    _reject_symlink_lexical_components(root_resolved, parts[:-1])
    parent = root_resolved.joinpath(*parts[:-1])
    parent.mkdir(parents=True, exist_ok=True)
    _secure_existing_directory(parent, label="artifact parent directory")
    candidate = root_resolved.joinpath(*parts)
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("artifact path escapes the protected artifact root") from exc


def resolve_run_relative_path(run_root: Path, relative_path: str) -> Path:
    parts = _validate_lexical_relative_path(relative_path)
    root_resolved = run_root.resolve(strict=False)
    _reject_symlink_lexical_components(root_resolved, parts)
    candidate = root_resolved.joinpath(*parts)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("relative_path escapes the run artifact root") from exc
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("relative_path escapes the run artifact root") from exc
    if resolved_candidate.exists() and resolved_candidate.is_symlink():
        raise ValueError("artifact path must not be a symlink")
    return resolved_candidate


def validate_sha256_hex(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return value

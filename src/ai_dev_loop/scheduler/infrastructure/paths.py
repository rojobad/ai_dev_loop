"""XDG path helpers for the central scheduler ledger."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode, state_dir

DEFAULT_DB_FILENAME = "engine.sqlite3"
ARTIFACTS_DIRNAME = "artifacts"
RUNS_DIRNAME = "runs"

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
    set_sensitive_file_mode(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            set_sensitive_file_mode(sidecar)


def safe_run_directory_key(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def run_artifact_root(artifact_root: Path, run_id: str) -> Path:
    return artifact_root / RUNS_DIRNAME / safe_run_directory_key(run_id)


def ensure_run_artifact_root(artifact_root: Path, run_id: str) -> Path:
    ensure_dir(artifact_root, mode=DIR_MODE)
    ensure_dir(artifact_root / RUNS_DIRNAME, mode=DIR_MODE)
    return ensure_dir(run_artifact_root(artifact_root, run_id), mode=DIR_MODE)


def resolve_run_relative_path(run_root: Path, relative_path: str) -> Path:
    if not relative_path or relative_path.startswith(("/", "\\")):
        raise ValueError("relative_path must be run-relative")
    if "\\" in relative_path:
        raise ValueError("relative_path must use forward slashes")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must be a lexical safe run-relative path")
    if not _SAFE_RELATIVE_PATH.match(relative_path):
        raise ValueError("relative_path contains unsafe characters")
    candidate = (run_root / relative_path).resolve(strict=False)
    root_resolved = run_root.resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("relative_path escapes the run artifact root") from exc
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("artifact path must not be a symlink")
    for parent in candidate.parents:
        if parent == root_resolved:
            break
        if parent.exists() and parent.is_symlink():
            raise ValueError("artifact path must not traverse a symlink")
    return candidate


def validate_sha256_hex(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    return value

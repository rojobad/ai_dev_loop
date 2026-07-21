"""XDG path helpers for the PR review v2 durable engine."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode, state_dir

PR_REVIEW_V2_DIRNAME = "pr-review-v2"
DEFAULT_DB_FILENAME = "engine.sqlite3"
ARTIFACTS_DIRNAME = "artifacts"
RUNS_DIRNAME = "runs"

_SAFE_RELATIVE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def pr_review_v2_state_dir() -> Path:
    return state_dir() / PR_REVIEW_V2_DIRNAME


def ensure_pr_review_v2_dir(path: Path | None = None) -> Path:
    target = path if path is not None else pr_review_v2_state_dir()
    return ensure_dir(target, mode=DIR_MODE)


def default_engine_db_path() -> Path:
    return pr_review_v2_state_dir() / DEFAULT_DB_FILENAME


def secure_database_path(db_path: Path) -> Path:
    """Ensure parent directory exists with user-only permissions and return the db path."""

    ensure_pr_review_v2_dir(db_path.parent)
    return db_path


def apply_database_permissions(db_path: Path) -> None:
    """Apply 0600 to the database file and known WAL/SHM sidecars where present."""

    set_sensitive_file_mode(db_path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            set_sensitive_file_mode(sidecar)


def safe_run_directory_key(run_id: str) -> str:
    """Derive a filesystem-safe run directory key (never raw run_id)."""

    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return digest


def run_artifact_root(artifact_root: Path, run_id: str) -> Path:
    """Return ``<artifact_root>/runs/<sha256(run_id)>`` without creating it."""

    return artifact_root / RUNS_DIRNAME / safe_run_directory_key(run_id)


def ensure_run_artifact_root(artifact_root: Path, run_id: str) -> Path:
    root = run_artifact_root(artifact_root, run_id)
    ensure_dir(artifact_root, mode=DIR_MODE)
    ensure_dir(artifact_root / RUNS_DIRNAME, mode=DIR_MODE)
    return ensure_dir(root, mode=DIR_MODE)


def resolve_run_relative_path(run_root: Path, relative_path: str) -> Path:
    """Resolve a lexical run-relative path and reject traversal/symlink escapes."""

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

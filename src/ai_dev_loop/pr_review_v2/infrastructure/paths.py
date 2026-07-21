"""XDG path helpers for the PR review v2 durable engine."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.paths import DIR_MODE, ensure_dir, set_sensitive_file_mode, state_dir

PR_REVIEW_V2_DIRNAME = "pr-review-v2"
DEFAULT_DB_FILENAME = "engine.sqlite3"


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

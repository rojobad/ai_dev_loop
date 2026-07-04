"""XDG path resolution and secure directory creation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "ai_dev_loop"
DIR_MODE = 0o700
SENSITIVE_FILE_MODE = 0o600

_dirs = PlatformDirs(appname=APP_NAME, appauthor=False)


def config_dir() -> Path:
    return Path(_dirs.user_config_dir)


def state_dir() -> Path:
    return Path(_dirs.user_state_dir)


def cache_dir() -> Path:
    return Path(_dirs.user_cache_dir)


def runs_dir() -> Path:
    return state_dir() / "runs"


def run_dir(project_slug: str, run_id: str) -> Path:
    return runs_dir() / project_slug / run_id


def global_config_path() -> Path:
    return config_dir() / "config.yaml"


def project_config_filename() -> str:
    return "ai_dev_loop.yaml"


def ensure_dir(path: Path, *, mode: int = DIR_MODE) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, mode)
    return path


def ensure_app_dirs() -> None:
    ensure_dir(config_dir())
    ensure_dir(state_dir())
    ensure_dir(cache_dir())
    ensure_dir(runs_dir())


def set_sensitive_file_mode(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, SENSITIVE_FILE_MODE)


def schema_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "schemas" / name


def repository_locks_dir() -> Path:
    return state_dir() / "repository-locks"


def repository_lock_path(repo_root: Path) -> Path:
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    return repository_locks_dir() / f"{digest}.lock"

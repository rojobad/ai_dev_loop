"""XDG path resolution and secure directory creation."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "ai_dev_loop"
DIR_MODE = 0o700
SENSITIVE_FILE_MODE = 0o600
CURSOR_CREDENTIAL_MAX_BYTES = 16 * 1024
_CURSOR_CREDENTIAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CURSOR_CONVERSATION_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

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


class CursorSdkPathError(ValueError):
    """Safe path/credential error whose text never includes secret material."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Cursor SDK local path validation failed ({code})")
        self.code = code


@dataclass(frozen=True)
class CursorSdkClientPaths:
    workspace: Path
    conversation_key: str
    conversation_root: Path
    state_root: Path
    store_root: Path


def cursor_credentials_dir() -> Path:
    return config_dir() / "credentials"


def cursor_sdk_state_root() -> Path:
    return state_dir() / "cursor-sdk"


def cursor_conversation_key(conversation_owner_identity: str) -> str:
    if not conversation_owner_identity or not conversation_owner_identity.strip():
        raise CursorSdkPathError("invalid_conversation_owner")
    return hashlib.sha256(conversation_owner_identity.encode("utf-8")).hexdigest()


def validate_cursor_conversation_key(value: str) -> str:
    if not _CURSOR_CONVERSATION_KEY_RE.fullmatch(value):
        raise CursorSdkPathError("invalid_conversation_key")
    return value


def _require_no_overlap(path: Path, repository_root: Path, *, code: str) -> None:
    candidate = path.resolve(strict=False)
    repository = repository_root.resolve(strict=False)
    try:
        candidate.relative_to(repository)
    except ValueError:
        pass
    else:
        raise CursorSdkPathError(code)
    try:
        repository.relative_to(candidate)
    except ValueError:
        return
    raise CursorSdkPathError(code)


def _require_owned_private_directory(path: Path, *, code: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise CursorSdkPathError(code)
    if os.name == "nt":
        return
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != DIR_MODE:
        raise CursorSdkPathError(code)


def _ensure_owned_private_directory(path: Path, *, code: str) -> Path:
    if path.is_symlink():
        raise CursorSdkPathError(code)
    if not path.exists():
        ensure_dir(path, mode=DIR_MODE)
    _require_owned_private_directory(path, code=code)
    return path


def resolve_cursor_credential_path(
    credential_ref: str,
    *,
    repository_root: Path | None = None,
) -> Path:
    if credential_ref in {".", ".."} or not _CURSOR_CREDENTIAL_REF_RE.fullmatch(credential_ref):
        raise CursorSdkPathError("unsafe_credential_ref")
    app_config_root = config_dir()
    credentials_root = cursor_credentials_dir()
    if repository_root is not None:
        _require_no_overlap(
            app_config_root,
            repository_root,
            code="credential_repository_overlap",
        )
    _require_owned_private_directory(app_config_root, code="unsafe_config_directory")
    _require_owned_private_directory(credentials_root, code="unsafe_credentials_directory")
    credential_path = credentials_root / credential_ref
    if credential_path.is_symlink():
        raise CursorSdkPathError("credential_symlink")
    try:
        metadata = credential_path.stat()
    except FileNotFoundError as exc:
        raise CursorSdkPathError("credential_missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CursorSdkPathError("credential_not_regular_file")
    if os.name != "nt" and (
        metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != SENSITIVE_FILE_MODE
    ):
        raise CursorSdkPathError("credential_insecure_permissions")
    if metadata.st_size > CURSOR_CREDENTIAL_MAX_BYTES:
        raise CursorSdkPathError("credential_too_large")
    try:
        credential_path.resolve(strict=True).relative_to(credentials_root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as exc:
        raise CursorSdkPathError("credential_escape") from exc
    return credential_path


def read_cursor_credential(
    credential_ref: str,
    *,
    repository_root: Path | None = None,
) -> str:
    path = resolve_cursor_credential_path(
        credential_ref,
        repository_root=repository_root,
    )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CursorSdkPathError("credential_unreadable") from exc
    if len(payload) > CURSOR_CREDENTIAL_MAX_BYTES:
        raise CursorSdkPathError("credential_too_large")
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CursorSdkPathError("credential_invalid_encoding") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        raise CursorSdkPathError("credential_invalid_payload")
    return value


def ensure_cursor_sdk_client_paths(
    *,
    workspace: Path,
    repository_root: Path,
    conversation_owner_identity: str,
    store_backend: str = "jsonl",
    native_store_layout_version: int = 1,
    require_existing_native_state: bool = False,
) -> CursorSdkClientPaths:
    if workspace.is_symlink() or not workspace.is_dir():
        raise CursorSdkPathError("invalid_workspace")
    workspace_resolved = workspace.resolve(strict=True)
    repository_resolved = repository_root.resolve(strict=True)
    try:
        workspace_resolved.relative_to(repository_resolved)
    except ValueError as exc:
        raise CursorSdkPathError("workspace_outside_repository") from exc
    key = validate_cursor_conversation_key(cursor_conversation_key(conversation_owner_identity))
    app_state_root = state_dir()
    sdk_root = cursor_sdk_state_root()
    conversation_root = sdk_root / key
    if native_store_layout_version != 1 or store_backend not in {"jsonl", "sqlite"}:
        raise CursorSdkPathError("unsupported_native_store_layout")
    bridge_state_root = (
        conversation_root if store_backend == "sqlite" else conversation_root / "bridge"
    )
    store_root = conversation_root if store_backend == "sqlite" else conversation_root / "store"
    _require_no_overlap(
        app_state_root,
        repository_resolved,
        code="state_repository_overlap",
    )
    required_directories = [
        (app_state_root, "unsafe_state_directory"),
        (sdk_root, "unsafe_sdk_state_directory"),
        (conversation_root, "unsafe_conversation_directory"),
    ]
    if store_backend == "jsonl":
        required_directories.extend(
            (
                (bridge_state_root, "unsafe_bridge_state_directory"),
                (store_root, "unsafe_store_directory"),
            )
        )
    for path, code in required_directories:
        if require_existing_native_state:
            try:
                _require_owned_private_directory(path, code=code)
            except CursorSdkPathError as exc:
                raise CursorSdkPathError("native_state_unavailable") from exc
        else:
            _ensure_owned_private_directory(path, code=code)
    if require_existing_native_state:
        if store_backend == "sqlite":
            marker = conversation_root / "index.db"
            if marker.is_symlink() or not marker.is_file():
                raise CursorSdkPathError("native_state_unavailable")
            try:
                if marker.stat().st_size < 100:
                    raise CursorSdkPathError("native_state_unavailable")
                uri = f"{marker.resolve(strict=True).as_uri()}?mode=ro"
                with sqlite3.connect(uri, uri=True) as connection:
                    quick_check = connection.execute("PRAGMA quick_check").fetchone()
                    page_count = connection.execute("PRAGMA page_count").fetchone()
                    persisted_table = connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type = 'table' LIMIT 1"
                    ).fetchone()
                if (
                    quick_check != ("ok",)
                    or page_count is None
                    or int(page_count[0]) < 1
                    or persisted_table is None
                ):
                    raise CursorSdkPathError("native_state_unavailable")
            except CursorSdkPathError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise CursorSdkPathError("native_state_unavailable") from exc
        else:
            try:
                next(store_root.iterdir())
            except (StopIteration, OSError) as exc:
                raise CursorSdkPathError("native_state_unavailable") from exc
    return CursorSdkClientPaths(
        workspace=workspace_resolved,
        conversation_key=key,
        conversation_root=conversation_root,
        state_root=bridge_state_root,
        store_root=store_root,
    )

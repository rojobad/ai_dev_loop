"""Run and repository lock management."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ai_dev_loop.errors import LockError
from ai_dev_loop.paths import ensure_dir, repository_lock_path


@dataclass(frozen=True)
class LockMetadata:
    pid: int
    run_id: str
    repository_path: str
    started_at: datetime


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def read_lock_metadata(path: Path) -> LockMetadata | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LockMetadata(
            pid=int(payload["pid"]),
            run_id=str(payload["run_id"]),
            repository_path=str(payload["repository_path"]),
            started_at=datetime.fromisoformat(str(payload["started_at"])),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


class FileLock:
    """Advisory lock using a lock file and fcntl on Linux/WSL."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self, metadata: LockMetadata) -> None:
        ensure_dir(self.path.parent)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            existing = read_lock_metadata(self.path)
            if existing is not None and not is_process_alive(existing.pid):
                self._handle = self.path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as retry_exc:
                    self._handle.close()
                    self._handle = None
                    raise LockError(_lock_held_message(self.path, existing)) from retry_exc
            elif existing is not None:
                raise LockError(_lock_held_message(self.path, existing)) from exc
            raise LockError(f"lock already held: {self.path}") from exc
        payload = {
            "pid": metadata.pid,
            "run_id": metadata.run_id,
            "repository_path": metadata.repository_path,
            "started_at": metadata.started_at.astimezone(UTC).isoformat(),
        }
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(payload, indent=2) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> FileLock:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any
    ) -> None:
        self.release()


def run_lock_path(run_directory: Path) -> Path:
    return run_directory / "locks" / "run.lock"


def _lock_held_message(path: Path, metadata: LockMetadata) -> str:
    return (
        f"lock already held: {path} "
        f"(pid={metadata.pid}, run_id={metadata.run_id}, "
        f"repository={metadata.repository_path}, started_at={metadata.started_at.isoformat()})"
    )


@dataclass
class RunLocks:
    """Acquire run and repository locks for a mutation command."""

    run_directory: Path
    metadata: LockMetadata
    _run_lock: FileLock | None = None
    _repo_lock: FileLock | None = None

    def acquire(self) -> None:
        self._run_lock = FileLock(run_lock_path(self.run_directory))
        self._run_lock.acquire(self.metadata)
        repo_path = Path(self.metadata.repository_path)
        self._repo_lock = FileLock(repository_lock_path(repo_path))
        try:
            self._repo_lock.acquire(self.metadata)
        except Exception:
            self._run_lock.release()
            self._run_lock = None
            self._repo_lock = None
            raise

    def release(self) -> None:
        if self._repo_lock is not None:
            self._repo_lock.release()
            self._repo_lock = None
        if self._run_lock is not None:
            self._run_lock.release()
            self._run_lock = None

    def __enter__(self) -> RunLocks:
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any
    ) -> None:
        self.release()

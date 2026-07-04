"""OS-level file locking primitives for future loop phases."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ai_dev_loop.paths import ensure_dir


@dataclass(frozen=True)
class LockMetadata:
    pid: int
    run_id: str
    repository_path: str
    started_at: datetime


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
            raise RuntimeError(f"lock already held: {self.path}") from exc
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

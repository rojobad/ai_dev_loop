"""Protected immutable artifact storage for scheduler runs."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.scheduler.infrastructure.paths import (
    ensure_run_artifact_root,
    resolve_run_relative_path,
    validate_sha256_hex,
)

MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_PROMPT_BYTES = 8 * 1024 * 1024
MAX_CONFIG_BYTES = 1 * 1024 * 1024
MAX_BASELINE_STATUS_BYTES = 1 * 1024 * 1024
MAX_SESSION_RUNTIME_BYTES = 64 * 1024


class ProtectedArtifactError(Exception):
    """Safe artifact read/write failure without sensitive content."""


@dataclass(frozen=True)
class StoredArtifact:
    relative_path: str
    sha256: str
    size_bytes: int


class ProtectedArtifactStore:
    """Write-once, hash-verified artifacts under the scheduler artifact root."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    def run_root(self, run_id: str) -> Path:
        return ensure_run_artifact_root(self.artifact_root, run_id)

    def write_bytes(
        self,
        run_id: str,
        relative_path: str,
        content: bytes,
        *,
        max_bytes: int,
    ) -> StoredArtifact:
        if not content:
            raise ProtectedArtifactError("artifact content is empty")
        if len(content) > max_bytes:
            raise ProtectedArtifactError("artifact exceeds size limit")
        root = self.run_root(run_id)
        destination = resolve_run_relative_path(root, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ProtectedArtifactError("artifact path already exists")
        digest = hashlib.sha256(content).hexdigest()
        fd, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".tmp-", suffix=".part")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, destination)
            if os.name != "nt":
                os.chmod(destination, SENSITIVE_FILE_MODE)
            verified = self.read_verified_bytes(run_id, relative_path, expected_sha256=digest)
            if verified != content:
                raise ProtectedArtifactError("post-write hash verification failed")
            return StoredArtifact(
                relative_path=relative_path,
                sha256=digest,
                size_bytes=len(content),
            )
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def write_text(
        self,
        run_id: str,
        relative_path: str,
        text: str,
        *,
        max_bytes: int,
    ) -> StoredArtifact:
        return self.write_bytes(
            run_id,
            relative_path,
            text.encode("utf-8"),
            max_bytes=max_bytes,
        )

    def write_yaml(
        self,
        run_id: str,
        relative_path: str,
        payload: dict[str, object],
        *,
        max_bytes: int = MAX_CONFIG_BYTES,
    ) -> StoredArtifact:
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        return self.write_text(run_id, relative_path, text, max_bytes=max_bytes)

    def read_verified_bytes(
        self,
        run_id: str,
        relative_path: str,
        *,
        expected_sha256: str,
    ) -> bytes:
        validate_sha256_hex(expected_sha256)
        root = self.run_root(run_id)
        path = resolve_run_relative_path(root, relative_path)
        if not path.is_file():
            raise ProtectedArtifactError("artifact missing")
        if path.is_symlink():
            raise ProtectedArtifactError("artifact must not be a symlink")
        if os.name != "nt":
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise ProtectedArtifactError("artifact has unsafe permissions")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256:
            raise ProtectedArtifactError("artifact hash mismatch")
        return data

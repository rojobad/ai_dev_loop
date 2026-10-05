"""Read-only confined access to scheduler protected artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ai_dev_loop.scheduler.infrastructure.paths import (
    readonly_confined_run_artifact_root,
    readonly_confined_sequence_artifact_root,
    resolve_run_relative_path,
    validate_sha256_hex,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactError


class ReadOnlyProtectedArtifactStore:
    """Hash-verified artifact reads without creating artifact directories."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root

    def run_root(self, run_id: str) -> Path:
        return readonly_confined_run_artifact_root(self.artifact_root, run_id)

    def sequence_root(self, sequence_id: str) -> Path:
        return readonly_confined_sequence_artifact_root(self.artifact_root, sequence_id)

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

    def list_run_relative_files(self, run_id: str) -> frozenset[str]:
        root = self.run_root(run_id)
        files: list[str] = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ProtectedArtifactError("run artifact tree must not contain symlinks")
            if path.is_file():
                files.append(path.relative_to(root).as_posix())
        return frozenset(files)

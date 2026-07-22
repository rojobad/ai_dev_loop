"""Atomic content-addressed writer for minimal PR review v2 write evidence.

Currently the only durable write evidence required by a domain outcome is the
trigger comment identity/time/hash needed for ``ReviewTriggerConfirmedOutcome``.
The writer reuses the Phase 16.5 run-root, ``0700`` directories, ``0600`` files,
atomic exclusive publication, and relative ``ArtifactRef`` conventions. Different
bytes for the same content-addressed path fail closed; identical bytes are a safe
idempotent reuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE, ensure_dir, set_sensitive_file_mode
from ai_dev_loop.pr_review_v2.application.write_contracts import TriggerEvidenceArtifact
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    resolve_run_relative_path,
    write_evidence_relative_path,
)

TRIGGER_EVIDENCE_KIND = "triggers"


class WriteEvidenceStoreError(Exception):
    """Raised when write-evidence persistence/verification fails."""

    def __init__(self, message: str = "write evidence persistence failed") -> None:
        super().__init__(message)


class WriteEvidenceStore:
    """Atomic protected content-addressed write-evidence writer/reader."""

    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root

    @property
    def root(self) -> Path:
        return self._root

    def persist_trigger_evidence(
        self, *, run_id: str, artifact: TriggerEvidenceArtifact
    ) -> ArtifactRef:
        payload = artifact.model_dump(mode="json")
        return self._persist(run_id=run_id, kind=TRIGGER_EVIDENCE_KIND, payload=payload)

    def _persist(self, *, run_id: str, kind: str, payload: dict[str, Any]) -> ArtifactRef:
        try:
            run_root = ensure_run_artifact_root(self._root, run_id)
            canonical = _canonical_json_bytes(payload)
            digest = hashlib.sha256(canonical).hexdigest()
            relative = write_evidence_relative_path(kind, digest)
            target = resolve_run_relative_path(run_root, relative)
            ensure_dir(target.parent, mode=DIR_MODE)
            if target.exists():
                existing = target.read_bytes()
                if existing != canonical:
                    raise WriteEvidenceStoreError(
                        "content-addressed write-evidence collision with different bytes"
                    )
                return ArtifactRef(relative_path=relative, sha256=digest)
            _atomic_write_bytes_exclusive(target, canonical)
            verified = hashlib.sha256(target.read_bytes()).hexdigest()
            if verified != digest:
                raise WriteEvidenceStoreError("post-write write-evidence hash mismatch")
            return ArtifactRef(relative_path=relative, sha256=digest)
        except WriteEvidenceStoreError:
            raise
        except OSError as exc:
            raise WriteEvidenceStoreError(
                "filesystem failure while writing write evidence"
            ) from exc
        except ValueError as exc:
            raise WriteEvidenceStoreError("write-evidence serialization failed") from exc


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (text + "\n").encode("utf-8")


def _atomic_write_bytes_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if hasattr(os, "chmod"):
            os.chmod(temp_path, SENSITIVE_FILE_MODE)
        try:
            os.link(temp_path, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != data:
                raise WriteEvidenceStoreError(
                    "content-addressed write-evidence collision with different bytes"
                ) from None
        except OSError:
            if path.exists():
                existing = path.read_bytes()
                if existing != data:
                    raise WriteEvidenceStoreError(
                        "content-addressed write-evidence collision with different bytes"
                    ) from None
            else:
                os.replace(temp_path, path)
        if path.exists():
            set_sensitive_file_mode(path)
            _fsync_dir(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _fsync_dir(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


__all__ = [
    "TRIGGER_EVIDENCE_KIND",
    "WriteEvidenceStore",
    "WriteEvidenceStoreError",
]

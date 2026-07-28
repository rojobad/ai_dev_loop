"""Protected hash-verifying readers for sensitive PR review v2 input artifacts.

These readers resolve every ``ArtifactRef`` beneath the Phase 16.5 hashed run
root by walking directory descriptors with ``O_NOFOLLOW`` / ``O_DIRECTORY``,
opening the final file relative to the verified parent descriptor, ``fstat``
the opened FD, stream-read with hard byte caps while hashing exact bytes, and
return opaque bytes or strict versioned typed schemas. Sensitive artifact
content never enters exception messages, logs, journal events, status, or SQLite.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from ai_dev_loop.pr_review_v2.application.contracts import AppModel
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
    DEFAULT_MAX_PATCH_BYTES,
    DEFAULT_MAX_TEXT_BYTES,
    AdoptedExistingPrPreimageArtifact,
    CommitMessageArtifact,
    PublicationTextArtifact,
    reject_prohibited_controls,
)
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    resolve_run_relative_path,
    run_artifact_root,
)

_TModel = TypeVar("_TModel", bound=AppModel)

_UNSAFE_MODE_BITS = 0o077  # any group/other permission bits (including world-readable 0644)
_READ_CHUNK = 64 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class InputArtifactErrorKind(StrEnum):
    MISSING = "missing"
    SYMLINK = "symlink"
    NOT_DIRECTORY = "not_directory"
    NOT_REGULAR = "not_regular"
    UNSAFE_MODE = "unsafe_mode"
    UNSAFE_PATH = "unsafe_path"
    HASH_MISMATCH = "hash_mismatch"
    OVERSIZE = "oversize"
    EMPTY = "empty"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_JSON = "invalid_json"
    SCHEMA = "schema"
    PROHIBITED_CONTROLS = "prohibited_controls"
    OPEN_FAILED = "open_failed"
    INVALID_MAX_BYTES = "invalid_max_bytes"
    INVALID_COMMITMENT = "invalid_commitment"


class InputArtifactError(Exception):
    """Protected artifact read/verification failure.

    The message is always a fixed safe classification. Sensitive content, paths
    relative to the caller's home, and hashes are never embedded.
    """

    def __init__(
        self,
        kind: InputArtifactErrorKind | str,
        reason: str | None = None,
    ) -> None:
        if isinstance(kind, InputArtifactErrorKind):
            self.kind = kind
            self.reason = reason if reason is not None else kind.value
        else:
            # Legacy string-only construction maps to OPEN_FAILED unless a known
            # reason is supplied as the sole argument during transitional calls.
            self.kind = InputArtifactErrorKind.OPEN_FAILED
            self.reason = kind
        super().__init__(self.reason)


def _dir_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # Avoid indefinite block on FIFOs / special files before the regular-file check.
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _open_relative(parent_fd: int, name: str, flags: int) -> int:
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise InputArtifactError(InputArtifactErrorKind.MISSING) from exc
    except OSError as exc:
        if getattr(exc, "errno", None) in {getattr(os, "ELOOP", 40), 40}:
            raise InputArtifactError(InputArtifactErrorKind.SYMLINK) from exc
        if getattr(exc, "errno", None) in {getattr(os, "ENOTDIR", 20), 20}:
            raise InputArtifactError(InputArtifactErrorKind.NOT_DIRECTORY) from exc
        raise InputArtifactError(InputArtifactErrorKind.OPEN_FAILED) from exc


def _open_under_run_root(run_root: Path, relative_path: str) -> int:
    """Open ``relative_path`` under ``run_root`` via no-follow directory descriptors."""

    # Lexical validation first (rejects ``..``, absolute, unsafe chars).
    try:
        resolve_run_relative_path(run_root, relative_path)
    except ValueError as exc:
        raise InputArtifactError(InputArtifactErrorKind.UNSAFE_PATH) from exc

    parts = relative_path.split("/")
    try:
        root_fd = os.open(os.fspath(run_root), _dir_flags())
    except FileNotFoundError as exc:
        raise InputArtifactError(InputArtifactErrorKind.MISSING) from exc
    except OSError as exc:
        if getattr(exc, "errno", None) in {getattr(os, "ELOOP", 40), 40}:
            raise InputArtifactError(InputArtifactErrorKind.SYMLINK) from exc
        raise InputArtifactError(InputArtifactErrorKind.OPEN_FAILED) from exc

    fd = root_fd
    try:
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            child = _open_relative(fd, part, _file_flags() if is_last else _dir_flags())
            os.close(fd)
            fd = child
            if not is_last:
                st = os.fstat(fd)
                if not stat.S_ISDIR(st.st_mode):
                    os.close(fd)
                    raise InputArtifactError(InputArtifactErrorKind.NOT_DIRECTORY)
        return fd
    except Exception:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise


def _verified_bytes_from_fd(
    fd: int,
    *,
    expected_sha256: str | None,
    max_bytes: int,
) -> bytes:
    if max_bytes <= 0:
        raise InputArtifactError(InputArtifactErrorKind.INVALID_MAX_BYTES)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InputArtifactError(InputArtifactErrorKind.NOT_REGULAR)
        if os.name != "nt" and (st.st_mode & _UNSAFE_MODE_BITS):
            raise InputArtifactError(InputArtifactErrorKind.UNSAFE_MODE)
        if st.st_size > max_bytes:
            raise InputArtifactError(
                InputArtifactErrorKind.OVERSIZE, "artifact exceeds maximum size"
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise InputArtifactError(
                    InputArtifactErrorKind.OVERSIZE, "artifact exceeds maximum size"
                )
            digest.update(chunk)
            chunks.append(chunk)
        raw = b"".join(chunks)
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise InputArtifactError(InputArtifactErrorKind.HASH_MISMATCH)
        return raw
    finally:
        os.close(fd)


def _verified_bytes(
    run_root: Path,
    relative_path: str,
    *,
    expected_sha256: str | None,
    max_bytes: int,
) -> bytes:
    fd = _open_under_run_root(run_root, relative_path)
    return _verified_bytes_from_fd(fd, expected_sha256=expected_sha256, max_bytes=max_bytes)


def _run_root(artifact_root: Path, run_id: str) -> Path:
    return run_artifact_root(artifact_root, run_id)


class InputArtifactReader:
    """Read protected input artifacts under a run's hashed artifact root."""

    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root

    def read_patch_bytes(
        self,
        *,
        run_id: str,
        ref: ArtifactRef,
        max_bytes: int = DEFAULT_MAX_PATCH_BYTES,
    ) -> bytes:
        run_root = _run_root(self._root, run_id)
        raw = _verified_bytes(
            run_root, ref.relative_path, expected_sha256=ref.sha256, max_bytes=max_bytes
        )
        if not raw:
            raise InputArtifactError(InputArtifactErrorKind.EMPTY, "staged patch artifact is empty")
        return raw

    def read_reply_text(
        self,
        *,
        run_id: str,
        ref: ArtifactRef,
        max_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    ) -> str:
        run_root = _run_root(self._root, run_id)
        raw = _verified_bytes(
            run_root, ref.relative_path, expected_sha256=ref.sha256, max_bytes=max_bytes
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputArtifactError(InputArtifactErrorKind.INVALID_UTF8) from exc
        try:
            reject_prohibited_controls(text, field_name="reply text")
        except ValueError as exc:
            raise InputArtifactError(InputArtifactErrorKind.PROHIBITED_CONTROLS) from exc
        if not text.strip():
            raise InputArtifactError(InputArtifactErrorKind.EMPTY, "reply artifact is empty")
        return text

    def read_commit_message(
        self,
        *,
        run_id: str,
        ref: ArtifactRef,
        max_bytes: int = DEFAULT_MAX_COMMIT_MESSAGE_BYTES,
    ) -> CommitMessageArtifact:
        run_root = _run_root(self._root, run_id)
        raw = _verified_bytes(
            run_root, ref.relative_path, expected_sha256=ref.sha256, max_bytes=max_bytes
        )
        return _load_json_model(raw, CommitMessageArtifact)

    def read_publication_text(
        self,
        *,
        run_id: str,
        ref: ArtifactRef,
        max_bytes: int = DEFAULT_MAX_TEXT_BYTES,
    ) -> PublicationTextArtifact:
        run_root = _run_root(self._root, run_id)
        raw = _verified_bytes(
            run_root, ref.relative_path, expected_sha256=ref.sha256, max_bytes=max_bytes
        )
        return _load_json_model(raw, PublicationTextArtifact)

    def read_adopted_existing_pr_preimage(
        self,
        *,
        run_id: str,
        ref: ArtifactRef,
        max_bytes: int = 1_048_576,
    ) -> AdoptedExistingPrPreimageArtifact:
        run_root = _run_root(self._root, run_id)
        raw = _verified_bytes(
            run_root, ref.relative_path, expected_sha256=ref.sha256, max_bytes=max_bytes
        )
        return _load_json_model(raw, AdoptedExistingPrPreimageArtifact)


def _load_json_model(raw: bytes, model: type[_TModel]) -> _TModel:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputArtifactError(InputArtifactErrorKind.INVALID_UTF8) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputArtifactError(InputArtifactErrorKind.INVALID_JSON) from exc
    try:
        return model.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - fail closed without leaking content
        raise InputArtifactError(InputArtifactErrorKind.SCHEMA) from exc


def read_verified_bytes_under_run_root(
    run_root: Path,
    relative_path: str,
    *,
    expected_sha256: str,
    max_bytes: int,
) -> bytes:
    """Race-safe owner-only hash-verified read beneath an already-resolved run root."""

    return _verified_bytes(
        run_root, relative_path, expected_sha256=expected_sha256, max_bytes=max_bytes
    )


def read_verified_sensitive_bytes(
    artifact_root: Path,
    *,
    run_id: str,
    relative_path: str,
    expected_sha256: str,
    max_bytes: int,
) -> bytes:
    """Race-safe owner-only hash-verified read beneath a run artifact root."""

    run_root = _run_root(artifact_root, run_id)
    return _verified_bytes(
        run_root, relative_path, expected_sha256=expected_sha256, max_bytes=max_bytes
    )


def read_owner_only_bounded_bytes(
    artifact_root: Path,
    *,
    run_id: str,
    relative_path: str,
    max_bytes: int,
) -> bytes:
    """Owner-only no-follow regular-file read without an external hash commitment."""

    run_root = _run_root(artifact_root, run_id)
    return _verified_bytes(run_root, relative_path, expected_sha256=None, max_bytes=max_bytes)


def read_content_commitment_digest(
    artifact_root: Path,
    *,
    run_id: str,
    relative_path: str,
) -> str:
    """Read a durable sha256 commitment file (64 lowercase hex chars + optional newline)."""

    raw = read_owner_only_bounded_bytes(
        artifact_root,
        run_id=run_id,
        relative_path=relative_path,
        max_bytes=65,
    )
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise InputArtifactError(InputArtifactErrorKind.INVALID_COMMITMENT) from exc
    if not _SHA256_HEX.fullmatch(text):
        raise InputArtifactError(InputArtifactErrorKind.INVALID_COMMITMENT)
    return text


__all__ = [
    "InputArtifactError",
    "InputArtifactErrorKind",
    "InputArtifactReader",
    "read_content_commitment_digest",
    "read_owner_only_bounded_bytes",
    "read_verified_bytes_under_run_root",
    "read_verified_sensitive_bytes",
]

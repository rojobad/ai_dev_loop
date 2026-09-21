"""Streaming digest verification for bounded scheduler artifacts."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ai_dev_loop.scheduler.infrastructure.paths import validate_sha256_hex

_READ_BUFFER_SIZE = 64 * 1024


def verify_file_digest(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
) -> int:
    """Verify a file's size and SHA-256 without loading the whole file into memory."""

    validate_sha256_hex(expected_sha256)
    if not path.is_file() or path.is_symlink():
        raise ValueError("artifact missing")
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(_READ_BUFFER_SIZE)
            if not block:
                break
            total += len(block)
            digest.update(block)
    if expected_size is not None and total != expected_size:
        raise ValueError("artifact size mismatch")
    if digest.hexdigest() != expected_sha256:
        raise ValueError("artifact hash mismatch")
    return total


def read_file_chunk(
    path: Path,
    *,
    byte_offset: int,
    limit: int,
    total_bytes: int,
) -> bytes:
    if byte_offset > total_bytes:
        raise ValueError("byte offset beyond artifact size")
    if byte_offset == total_bytes:
        return b""
    read_limit = min(limit, total_bytes - byte_offset)
    with path.open("rb") as handle:
        handle.seek(byte_offset)
        return handle.read(read_limit)

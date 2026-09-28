"""Bounded byte reads for live or completed child process capture files."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.validation import (
    ARTIFACT_HARD_MAX_LIMIT,
    validate_artifact_read_bounds,
)


@dataclass(frozen=True)
class ProcessOutputChunk:
    byte_offset: int
    returned_bytes: int
    available_bytes: int
    next_offset: int | None
    has_more: bool
    complete: bool
    content_base64: str


@dataclass(frozen=True)
class CaptureSnapshot:
    inode: int
    mtime_ns: int
    size: int


def _snapshot_from_path(path: Path) -> CaptureSnapshot:
    stat = path.stat()
    return CaptureSnapshot(inode=stat.st_ino, mtime_ns=stat.st_mtime_ns, size=stat.st_size)


def assert_stable_capture_file(
    path: Path, *, snapshot: CaptureSnapshot, writer_stopped: bool
) -> None:
    after = path.stat()
    if after.st_size < snapshot.size:
        raise IntegrationApiError.data_integrity("Process output stream changed during read.")
    if after.st_ino != snapshot.inode:
        raise IntegrationApiError.data_integrity("Process output stream changed during read.")
    if writer_stopped:
        if after.st_size > snapshot.size:
            raise IntegrationApiError.data_integrity("Process output stream changed during read.")
        if after.st_mtime_ns != snapshot.mtime_ns:
            raise IntegrationApiError.data_integrity("Process output stream changed during read.")


def read_process_output_chunk(
    path: Path,
    *,
    byte_offset: int,
    limit: int,
    writer_stopped: bool,
    size_before_read: int | None = None,
    snapshot: CaptureSnapshot | None = None,
) -> ProcessOutputChunk:
    validate_artifact_read_bounds(byte_offset, limit)
    if byte_offset < 0:
        raise IntegrationApiError.invalid_argument("byteOffset must be non-negative.")
    if not path.is_file():
        raise IntegrationApiError.data_integrity("Process output stream is missing.")
    observed_snapshot = snapshot if snapshot is not None else _snapshot_from_path(path)
    observed = size_before_read if size_before_read is not None else observed_snapshot.size
    if byte_offset > observed:
        raise IntegrationApiError.invalid_argument("byteOffset is beyond the stream size.")
    if byte_offset == observed:
        assert_stable_capture_file(path, snapshot=observed_snapshot, writer_stopped=writer_stopped)
        chunk = b""
    else:
        read_limit = min(limit, ARTIFACT_HARD_MAX_LIMIT, observed - byte_offset)
        with path.open("rb") as handle:
            handle.seek(byte_offset)
            chunk = handle.read(read_limit)
        assert_stable_capture_file(path, snapshot=observed_snapshot, writer_stopped=writer_stopped)

    end_offset = byte_offset + len(chunk)
    has_more = end_offset < observed
    complete = writer_stopped
    if has_more:
        next_offset: int | None = end_offset
    elif writer_stopped:
        next_offset = None
    else:
        next_offset = end_offset

    return ProcessOutputChunk(
        byte_offset=byte_offset,
        returned_bytes=len(chunk),
        available_bytes=observed,
        next_offset=next_offset,
        has_more=has_more,
        complete=complete,
        content_base64=base64.b64encode(chunk).decode("ascii"),
    )

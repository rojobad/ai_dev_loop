"""Unit tests for process output chunk projection and reader (Phase 21.5)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.process_output_projection import build_process_output_chunk
from ai_dev_loop.integration_api.process_output_reader import (
    CaptureSnapshot,
    assert_stable_capture_file,
    read_process_output_chunk,
)


def test_c02_join_chunks_match_utf8_including_multibyte_split(tmp_path: Path) -> None:
    payload = "café".encode()
    path = tmp_path / "stream.txt"
    path.write_bytes(payload)
    first = read_process_output_chunk(
        path,
        byte_offset=0,
        limit=3,
        writer_stopped=True,
    )
    second = read_process_output_chunk(
        path,
        byte_offset=first.next_offset or 0,
        limit=16,
        writer_stopped=True,
        size_before_read=len(payload),
    )
    joined = base64.b64decode(first.content_base64) + base64.b64decode(second.content_base64)
    assert joined == payload
    assert first.has_more is True
    assert first.complete is True
    assert second.complete is True
    assert second.has_more is False
    assert second.next_offset is None


def test_c02_resolved_writer_complete_with_more_chunks(tmp_path: Path) -> None:
    path = tmp_path / "stream.txt"
    path.write_bytes(b"abcdef")
    chunk = read_process_output_chunk(
        path,
        byte_offset=0,
        limit=3,
        writer_stopped=True,
    )
    assert chunk.complete is True
    assert chunk.has_more is True
    assert chunk.next_offset == 3
    assert chunk.available_bytes == 6


def test_c02_zero_byte_file_eof_while_unresolved(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    chunk = read_process_output_chunk(
        path,
        byte_offset=0,
        limit=64,
        writer_stopped=False,
    )
    assert chunk.returned_bytes == 0
    assert chunk.complete is False
    assert chunk.has_more is False
    assert chunk.next_offset == 0


def test_c02_uncertain_writer_incomplete_at_eof(tmp_path: Path) -> None:
    path = tmp_path / "stream.txt"
    path.write_bytes(b"done")
    chunk = read_process_output_chunk(
        path,
        byte_offset=4,
        limit=64,
        writer_stopped=False,
        size_before_read=4,
    )
    assert chunk.complete is False
    assert chunk.next_offset == 4


def test_c02_offset_beyond_size_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tiny.txt"
    path.write_bytes(b"ab")
    with pytest.raises(IntegrationApiError):
        read_process_output_chunk(path, byte_offset=3, limit=1, writer_stopped=True)


def test_f04_active_writer_allows_append_growth(tmp_path: Path) -> None:
    path = tmp_path / "grow.txt"
    path.write_bytes(b"ab")
    snapshot = CaptureSnapshot(
        inode=path.stat().st_ino,
        mtime_ns=path.stat().st_mtime_ns,
        size=2,
    )
    path.write_bytes(b"abcd")
    assert_stable_capture_file(path, snapshot=snapshot, writer_stopped=False)
    chunk = read_process_output_chunk(
        path,
        byte_offset=0,
        limit=8,
        writer_stopped=False,
        size_before_read=2,
        snapshot=snapshot,
    )
    assert chunk.available_bytes == 2
    assert chunk.returned_bytes == 2
    assert chunk.complete is False


def test_f04_active_writer_rejects_inode_replacement(tmp_path: Path) -> None:
    path = tmp_path / "live.txt"
    path.write_bytes(b"data")
    snapshot = CaptureSnapshot(
        inode=path.stat().st_ino,
        mtime_ns=path.stat().st_mtime_ns,
        size=4,
    )
    path.rename(tmp_path / "moved-aside.txt")
    path.write_bytes(b"data")
    with pytest.raises(IntegrationApiError) as exc:
        assert_stable_capture_file(path, snapshot=snapshot, writer_stopped=False)
    assert exc.value.code.name == "DATA_INTEGRITY"


def test_f04_same_size_replacement_during_read_is_data_integrity(tmp_path: Path) -> None:
    path = tmp_path / "stream.txt"
    path.write_bytes(b"aaaaa")
    snapshot = CaptureSnapshot(
        inode=path.stat().st_ino,
        mtime_ns=path.stat().st_mtime_ns,
        size=5,
    )
    path.write_bytes(b"bbbbb")
    stale = CaptureSnapshot(
        inode=snapshot.inode,
        mtime_ns=snapshot.mtime_ns - 1,
        size=snapshot.size,
    )
    with pytest.raises(IntegrationApiError) as exc:
        assert_stable_capture_file(path, snapshot=stale, writer_stopped=True)
    assert exc.value.code.name == "DATA_INTEGRITY"


def test_f04_eof_empty_read_detects_shrink(tmp_path: Path) -> None:
    path = tmp_path / "stream.txt"
    path.write_bytes(b"xy")
    snapshot = CaptureSnapshot(
        inode=path.stat().st_ino,
        mtime_ns=path.stat().st_mtime_ns,
        size=5,
    )
    with pytest.raises(IntegrationApiError) as exc:
        read_process_output_chunk(
            path,
            byte_offset=5,
            limit=8,
            writer_stopped=True,
            size_before_read=5,
            snapshot=snapshot,
        )
    assert exc.value.code.name == "DATA_INTEGRITY"


def test_c02_shrink_during_read_is_data_integrity(tmp_path: Path) -> None:
    path = tmp_path / "grow.txt"
    path.write_bytes(b"hello")
    snapshot = CaptureSnapshot(
        inode=path.stat().st_ino,
        mtime_ns=path.stat().st_mtime_ns,
        size=10,
    )
    with pytest.raises(IntegrationApiError) as exc:
        read_process_output_chunk(
            path,
            byte_offset=0,
            limit=10,
            writer_stopped=True,
            size_before_read=10,
            snapshot=snapshot,
        )
    assert exc.value.code.name == "DATA_INTEGRITY"


def test_c02_available_bytes_stays_pre_read_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "stream.txt"
    path.write_bytes(b"abc")
    chunk = read_process_output_chunk(
        path,
        byte_offset=0,
        limit=2,
        writer_stopped=False,
        size_before_read=3,
    )
    assert chunk.available_bytes == 3
    assert chunk.returned_bytes == 2


def test_c02_unavailable_chunk_wire_shape() -> None:
    wire = build_process_output_chunk(
        run_id="run-1",
        attempt_id="att-1",
        component="codex",
        stream="stdout",
        stream_format="jsonl",
        process_state="active",
        complete=False,
        truncated_at_source=None,
        unavailable_reason="not_yet_produced",
    )
    dumped = wire.model_dump(by_alias=True)
    assert dumped["available"] is False
    assert dumped["reason"] == "not_yet_produced"
    assert dumped["returnedBytes"] == 0

"""Unit tests for lock helpers."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dev_loop.errors import LockError
from ai_dev_loop.locking import (
    FileLock,
    LockMetadata,
    RunLocks,
    is_process_alive,
    read_lock_metadata,
    run_lock_path,
)
from ai_dev_loop.paths import repository_lock_path


def test_is_process_alive_for_current_pid() -> None:
    assert is_process_alive(os.getpid()) is True


def test_is_process_alive_for_missing_pid() -> None:
    assert is_process_alive(999999999) is False


def test_read_lock_metadata_round_trip(tmp_path: Path) -> None:
    started = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    metadata = LockMetadata(
        pid=1234,
        run_id="demo-run",
        repository_path="/tmp/repo",
        started_at=started,
    )
    lock_path = tmp_path / "run.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": metadata.pid,
                "run_id": metadata.run_id,
                "repository_path": metadata.repository_path,
                "started_at": started.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    loaded = read_lock_metadata(lock_path)
    assert loaded == metadata


def test_run_lock_prevents_concurrent_acquire(tmp_path: Path) -> None:
    started = datetime.now(tz=UTC)
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id="run-a",
        repository_path=str(tmp_path / "repo"),
        started_at=started,
    )
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / "locks").mkdir()

    first = RunLocks(run_directory, metadata)
    first.acquire()
    second = RunLocks(run_directory, metadata)
    with pytest.raises(LockError, match="lock already held"):
        second.acquire()
    first.release()


def test_repository_lock_path_is_stable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert repository_lock_path(repo) == repository_lock_path(repo.resolve())


def test_file_lock_release_allows_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "demo.lock"
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id="demo",
        repository_path=str(tmp_path),
        started_at=datetime.now(tz=UTC),
    )
    lock = FileLock(lock_path)
    lock.acquire(metadata)
    lock.release()
    lock.acquire(metadata)
    lock.release()


def test_run_lock_released_when_repository_lock_fails(tmp_path: Path) -> None:
    run_directory = tmp_path / "run-a"
    (run_directory / "locks").mkdir(parents=True)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    started = datetime.now(tz=UTC)
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id="run-a",
        repository_path=str(repo_path),
        started_at=started,
    )
    other_metadata = LockMetadata(
        pid=os.getpid(),
        run_id="run-b",
        repository_path=str(repo_path),
        started_at=started,
    )

    repo_holder = FileLock(repository_lock_path(repo_path))
    repo_holder.acquire(other_metadata)
    try:
        locks = RunLocks(run_directory, metadata)
        with pytest.raises(LockError):
            locks.acquire()

        run_lock = FileLock(run_lock_path(run_directory))
        run_lock.acquire(metadata)
        run_lock.release()
    finally:
        repo_holder.release()

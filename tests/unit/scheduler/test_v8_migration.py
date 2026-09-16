"""Unit tests for scheduler schema v8 migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _pause_v8_database(tmp_path: Path) -> Path:
    db = tmp_path / "v8.sqlite3"
    paused = False

    def pause_v9(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_fresh_review_recoveries" in statement:
            paused = True
            raise RuntimeError("pause-v9")

    with pytest.raises(RuntimeError, match="pause-v9"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v9)
    assert _user_version(db) == 8
    return db


def test_v8_migration_adds_capacity_retry_tables(tmp_path: Path) -> None:
    db = _pause_v8_database(tmp_path)
    store = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION
    with store.begin_read() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "scheduler_capacity_retry_generations" in tables
        assert "scheduler_fresh_review_recoveries" in tables
        assert "scheduler_sequence_recovery_resolutions" in tables

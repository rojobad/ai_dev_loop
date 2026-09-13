"""Unit tests for scheduler schema v6 migration."""

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


def _pause_v5_database(tmp_path: Path) -> Path:
    db = tmp_path / "v5.sqlite3"
    paused = False

    def pause_v6(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_review_retry_generations" in statement:
            paused = True
            raise RuntimeError("pause-v6")

    with pytest.raises(RuntimeError, match="pause-v6"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v6)
    assert _user_version(db) == 5
    return db


def test_v6_migration_adds_review_retry_tables(tmp_path: Path) -> None:
    db = _pause_v5_database(tmp_path)
    store = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION == 6
    with store.begin_read() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "scheduler_review_retry_generations" in tables
        assert "scheduler_review_recovery_successors" in tables

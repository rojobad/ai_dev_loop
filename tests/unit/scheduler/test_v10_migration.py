"""Unit tests for scheduler schema v10 migration."""

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


def _pause_v7_database(tmp_path: Path) -> Path:
    db = tmp_path / "v7.sqlite3"
    paused = False

    def pause_v8(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_capacity_retry_generations" in statement:
            paused = True
            raise RuntimeError("pause-v8")

    with pytest.raises(RuntimeError, match="pause-v8"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v8)
    assert _user_version(db) == 7
    return db


def _pause_v9_database(tmp_path: Path) -> Path:
    db = tmp_path / "v9.sqlite3"
    paused = False

    def pause_v10(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_authenticated_rollovers" in statement:
            paused = True
            raise RuntimeError("pause-v10")

    with pytest.raises(RuntimeError, match="pause-v10"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v10)
    assert _user_version(db) == 9
    return db


def test_v7_bootstrap_reaches_schema_version_10_with_rollover_tables(tmp_path: Path) -> None:
    db = _pause_v7_database(tmp_path)
    store = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION == 10
    with store.begin_read() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "scheduler_checkpoint_holds" in tables
        assert "scheduler_capacity_retry_generations" in tables
        assert "scheduler_authenticated_rollovers" in tables
        assert "scheduler_authenticated_rollover_idempotency" in tables
        assert "scheduler_sequence_rollover_resolutions" in tables


def test_v10_migration_adds_rollover_tables(tmp_path: Path) -> None:
    db = _pause_v9_database(tmp_path)
    store = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION == 10
    with store.begin_read() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "scheduler_authenticated_rollovers" in tables
        assert "scheduler_authenticated_rollover_idempotency" in tables
        assert "scheduler_sequence_rollover_resolutions" in tables

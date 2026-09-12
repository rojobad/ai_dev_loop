"""Unit tests for scheduler schema v4 migration."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import sample_submitted_state

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    SCHEMA_VERSION,
    SqliteSchedulerStore,
)


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _pause_v3_database(tmp_path: Path) -> Path:
    db = tmp_path / "v3.sqlite3"
    paused = False

    def pause_v4(statement: str) -> None:
        nonlocal paused
        if not paused and "ALTER TABLE scheduler_attempts ADD COLUMN ingested" in statement:
            paused = True
            raise RuntimeError("pause-v4")

    with pytest.raises(RuntimeError, match="pause-v4"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v4)
    assert _user_version(db) == 3
    return db


def _populate_v3_fixture(db: Path) -> dict[str, int]:
    store = SqliteSchedulerStore(db, bootstrap=False)
    state = sample_submitted_state(repo_root="/tmp/v3-repo")
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-v3-submit",
            event=event,
            now=now,
        )
        counts = {
            "runs": conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()[0],
            "effects": conn.execute("SELECT COUNT(*) FROM scheduler_effects").fetchone()[0],
            "attempts": conn.execute("SELECT COUNT(*) FROM scheduler_attempts").fetchone()[0],
        }
    return counts


def test_v4_migration_preserves_v3_rows(tmp_path: Path) -> None:
    db = _pause_v3_database(tmp_path)
    before = _populate_v3_fixture(db)
    store = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION == 5
    with store.begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == before["runs"]
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()[0] == before["events"]
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(scheduler_attempts)").fetchall()
        }
        assert "ingested" in columns


def test_v4_migration_checksum_rejected_on_reopen(tmp_path: Path) -> None:
    db = _pause_v3_database(tmp_path)
    _populate_v3_fixture(db)
    SqliteSchedulerStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE scheduler_schema_migrations SET checksum = ? WHERE version = 4",
        ("f" * 64,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchedulerEngineError, match="checksum"):
        SqliteSchedulerStore(db)


def test_v4_migration_rollback_on_fault(tmp_path: Path) -> None:
    paused_db = _pause_v3_database(tmp_path)
    _populate_v3_fixture(paused_db)

    def boom(statement: str) -> None:
        if "ingested" in statement:
            raise RuntimeError("injected v4 fault")

    with pytest.raises(RuntimeError, match="injected v4 fault"):
        SqliteSchedulerStore(paused_db, migration_fault_hook=boom)
    assert _user_version(paused_db) == 3
    conn = sqlite3.connect(paused_db)
    try:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(scheduler_attempts)").fetchall()
        }
        assert "ingested" not in columns
    finally:
        conn.close()

"""Unit tests for scheduler SQLite bootstrap and CAS."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import sample_submitted_state

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.paths import DEFAULT_DB_FILENAME, scheduler_state_dir
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    CAPACITY_NAME,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    SqliteSchedulerStore,
    migration_checksum,
)


def test_default_xdg_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert scheduler_state_dir() == tmp_path / "state" / "ai_dev_loop"
    assert (scheduler_state_dir() / DEFAULT_DB_FILENAME).name == DEFAULT_DB_FILENAME


def test_bootstrap_empty_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    assert db.exists()
    with store.begin_read() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert tables >= REQUIRED_TABLES
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL"
            ).fetchall()
        }
        assert indexes >= REQUIRED_INDEXES
        row = conn.execute(
            "SELECT checksum FROM scheduler_schema_migrations WHERE version=1"
        ).fetchone()
        assert row[0] == migration_checksum(1)
        row2 = conn.execute(
            "SELECT checksum FROM scheduler_schema_migrations WHERE version=2"
        ).fetchone()
        assert row2[0] == migration_checksum(2)
        row3 = conn.execute(
            "SELECT checksum FROM scheduler_schema_migrations WHERE version=3"
        ).fetchone()
        assert row3 is not None
        assert row3[0] == migration_checksum(3)
        capacity = conn.execute(
            "SELECT max_value FROM scheduler_capacity WHERE capacity_name = ?",
            (CAPACITY_NAME,),
        ).fetchone()
        assert capacity is not None
        assert int(capacity[0]) == 1
    SqliteSchedulerStore(db)


def test_future_version_rejected(tmp_path: Path) -> None:
    db = tmp_path / "future.sqlite3"
    SqliteSchedulerStore(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchedulerEngineError, match="newer than supported"):
        SqliteSchedulerStore(db)


def test_checksum_drift_rejected(tmp_path: Path) -> None:
    db = tmp_path / "drift.sqlite3"
    SqliteSchedulerStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE scheduler_schema_migrations SET checksum = ? WHERE version = 1",
        ("0" * 64,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchedulerEngineError, match="checksum"):
        SqliteSchedulerStore(db)


def test_mid_migration_fault_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "partial-migrate.sqlite3"

    def boom(statement: str) -> None:
        normalized = " ".join(statement.split())
        if normalized.startswith("CREATE TABLE scheduler_effects"):
            raise RuntimeError("injected mid-migration fault")

    with pytest.raises(RuntimeError, match="mid-migration"):
        SqliteSchedulerStore(db, migration_fault_hook=boom)
    assert _user_version(db) == 0
    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert "scheduler_schema_migrations" not in tables


def test_database_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    db = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(db)
    assert (db.stat().st_mode & 0o777) == 0o600
    assert (db.parent.stat().st_mode & 0o777) == 0o700


def test_foreign_key_invariants_on_submission(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state()
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
            event_id="evt-1",
            event=event,
            now=now,
        )
    with store.begin_read() as conn:
        reservation = conn.execute(
            "SELECT run_id FROM scheduler_repository_reservations WHERE worktree_key = ?",
            (state.context.repository.worktree_key,),
        ).fetchone()
        assert reservation is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM scheduler_runs WHERE run_id = ?", (state.run_id,))


def test_compare_and_swap_state(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state()
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
            event_id="evt-1",
            event=event,
            now=now,
        )
    updated = state.model_copy(update={"version": 2, "updated_at": "2026-09-04T12:01:00.000000Z"})
    with store.begin_immediate() as conn:
        assert store.compare_and_swap_state(
            conn,
            run_id=state.run_id,
            expected_version=1,
            new_state=updated,
            now=datetime(2026, 9, 4, 12, 1, 0, tzinfo=UTC),
        )
        assert not store.compare_and_swap_state(
            conn,
            run_id=state.run_id,
            expected_version=1,
            new_state=updated,
            now=datetime(2026, 9, 4, 12, 2, 0, tzinfo=UTC),
        )


def _user_version(db: Path) -> int:
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()

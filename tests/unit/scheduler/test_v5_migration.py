"""Unit tests for scheduler schema v5 migration."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_NOW,
    FIXED_SEQUENCE_ID,
    _two_phase_manifest,
    _write_manifest,
)

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    SequencePrepareService,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _pause_v4_database(tmp_path: Path) -> Path:
    db = tmp_path / "v4.sqlite3"
    paused = False

    def pause_v5(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_sequences" in statement:
            paused = True
            raise RuntimeError("pause-v5")

    with pytest.raises(RuntimeError, match="pause-v5"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v5)
    assert _user_version(db) == 4
    return db


def test_v5_migration_adds_sequence_tables(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)
    store = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION == 5
    with store.begin_read() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "scheduler_sequences" in tables
        assert "scheduler_sequence_entries" in tables


def test_v5_migration_rollback_on_fault(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)

    def boom(statement: str) -> None:
        if "scheduler_sequence_entries" in statement:
            raise RuntimeError("injected v5 fault")

    with pytest.raises(RuntimeError, match="injected v5 fault"):
        SqliteSchedulerStore(db, migration_fault_hook=boom)
    assert _user_version(db) == 4


def test_v5_migration_checksum_rejected_on_reopen(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)
    SqliteSchedulerStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE scheduler_schema_migrations SET checksum = ? WHERE version = 5",
        ("f" * 64,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchedulerEngineError, match="checksum"):
        SqliteSchedulerStore(db)


def test_v4_database_remains_readable_after_v5_migration(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    db = _pause_v4_database(tmp_path)
    SqliteSchedulerStore(db)
    artifact_root = tmp_path / "artifacts"
    manifest = _write_manifest(tmp_path / "sequence.yaml", _two_phase_manifest())
    run_counter = {"value": 0}

    def unique_run_id(_slug: str, _now: datetime) -> str:
        index = run_counter["value"]
        run_counter["value"] += 1
        return f"fixture-project-20260912T120000Z-run{index:03d}"

    service = SequencePrepareService(
        SqliteSchedulerStore(db),
        ProtectedArtifactStore(artifact_root),
        repository_discoverer=lambda _path: RepositoryTarget(root=git_repo.resolve()),
        now_factory=lambda: FIXED_NOW,
        sequence_id_factory=lambda _slug, _now: FIXED_SEQUENCE_ID,
        run_id_factory=unique_run_id,
    )
    service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifact_root,
        )
    )

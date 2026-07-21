"""Unit tests for Phase 16.4 SQLite schema bootstrap and permissions."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ai_dev_loop.pr_review_v2.application.contracts import PrReviewEngineError
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    DEFAULT_DB_FILENAME,
    default_engine_db_path,
    pr_review_v2_state_dir,
)
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import (
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    SqlitePrReviewStore,
    migration_checksum,
)


def test_default_xdg_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert pr_review_v2_state_dir() == tmp_path / "state" / "ai_dev_loop" / "pr-review-v2"
    assert default_engine_db_path().name == DEFAULT_DB_FILENAME


def test_bootstrap_empty_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqlitePrReviewStore(db)
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
            "SELECT checksum FROM pr_review_schema_migrations WHERE version=1"
        ).fetchone()
        assert row[0] == migration_checksum()
    # reopen
    SqlitePrReviewStore(db)


def test_future_version_rejected(tmp_path: Path) -> None:
    db = tmp_path / "future.sqlite3"
    SqlitePrReviewStore(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(PrReviewEngineError, match="newer than supported"):
        SqlitePrReviewStore(db)


def test_partial_schema_rejected(tmp_path: Path) -> None:
    db = tmp_path / "partial.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE weird(x INTEGER)")
    conn.execute("PRAGMA user_version = 0")
    conn.close()
    with pytest.raises(PrReviewEngineError, match="non-empty v0"):
        SqlitePrReviewStore(db)


def test_checksum_drift_rejected(tmp_path: Path) -> None:
    db = tmp_path / "drift.sqlite3"
    SqlitePrReviewStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE pr_review_schema_migrations SET checksum = ? WHERE version = 1",
        ("0" * 64,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(PrReviewEngineError, match="checksum"):
        SqlitePrReviewStore(db)


def test_database_permissions(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    db = tmp_path / "engine.sqlite3"
    SqlitePrReviewStore(db)
    assert (db.stat().st_mode & 0o777) == 0o600
    assert (db.parent.stat().st_mode & 0o777) == 0o700
    # touch WAL sidecars by writing
    with SqlitePrReviewStore(db).begin_immediate() as conn:
        conn.execute("SELECT 1")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db}{suffix}")
        if sidecar.exists():
            assert (sidecar.stat().st_mode & 0o777) == 0o600


def test_mid_migration_fault_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "partial-migrate.sqlite3"

    def boom(statement: str) -> None:
        normalized = " ".join(statement.split())
        if normalized.startswith("CREATE TABLE pr_review_effects"):
            raise RuntimeError("injected mid-migration fault")

    with pytest.raises(RuntimeError, match="mid-migration"):
        SqlitePrReviewStore(db, migration_fault_hook=boom)
    assert _user_version(db) == 0
    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert "pr_review_worker_leases" not in tables
    assert "pr_review_schema_migrations" not in tables


def _user_version(db: Path) -> int:
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_unique_live_dispatch_constraint(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqlitePrReviewStore(db)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO pr_review_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, created_at, updated_at
            ) VALUES ('r1', 'prepared', '{}', ?, 1, '2026-07-20T12:00:00.000000Z',
                      '2026-07-20T12:00:00.000000Z')
            """,
            ("a" * 64,),
        )
        conn.execute(
            """
            INSERT INTO pr_review_events(
                event_id, run_id, sequence, event_kind, event_payload,
                event_payload_sha256, disposition, expected_run_version,
                observed_run_version, resulting_run_version,
                resulting_state_payload, resulting_state_payload_sha256,
                rejection_code, safe_detail, created_at
            ) VALUES (
                'e1', 'r1', 1, 'start_requested', '{}', ?, 'accepted', 1, 1, 2,
                '{}', ?, NULL, NULL, '2026-07-20T12:00:00.000000Z'
            )
            """,
            ("b" * 64, "e" * 64),
        )
        base = (
            "d1",
            "e1",
            0,
            "r1",
            "eff-1",
            "idem-1",
            "generate_publication_text",
            "{}",
            "c" * 64,
            "local",
            1,
            1,
            "pending",
            "2026-07-20T12:00:00.000000Z",
            "2026-07-20T12:00:00.000000Z",
            "2026-07-20T12:00:00.000000Z",
        )
        conn.execute(
            """
            INSERT INTO pr_review_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                classification, attempt, max_attempts, status, available_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            base,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO pr_review_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    classification, attempt, max_attempts, status, available_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "d2",
                    "e1",
                    1,
                    "r1",
                    "eff-2",
                    "idem-2",
                    "commit_patch",
                    "{}",
                    "d" * 64,
                    "mutating",
                    1,
                    6,
                    "pending",
                    "2026-07-20T12:00:00.000000Z",
                    "2026-07-20T12:00:00.000000Z",
                    "2026-07-20T12:00:00.000000Z",
                ),
            )

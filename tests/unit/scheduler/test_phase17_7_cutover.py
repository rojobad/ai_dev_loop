"""Unit tests for Phase 17.7 legacy state cutover cleanup."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.locking import FileLock, LockMetadata
from ai_dev_loop.scheduler.application.cutover_cleanup import (
    CUTOVER_CONFIRMATION_TOKEN,
    CUTOVER_TICK_OWNER,
    _cutover_coordination,
    _cutover_lock_path,
    _delete_legacy_tree,
    cutover_cleanup_blocks_scheduler_tick,
    resolve_legacy_target,
    run_cutover_cleanup,
    scheduler_active_work_reason,
    validate_state_root,
)
from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.application.tick_fencing import parse_utc_instant
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _init_scheduler_db(db_path: Path) -> SqliteSchedulerStore:
    return SqliteSchedulerStore(db_path)


def test_cutover_requires_confirmation_token(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    with pytest.raises(ValidationError, match="confirmation token"):
        run_cutover_cleanup(
            confirmation_token="wrong-token",
            state_root=state_root,
            db_path=state_root / "engine.sqlite3",
        )


def test_cutover_deletes_only_exact_legacy_roots(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    runs = state_root / "runs" / "fixture-project" / "run-1"
    pr_review = state_root / "pr-review-v2" / "engine.sqlite3"
    config = state_root / "config.yaml"
    artifacts = state_root / "artifacts" / "runs" / "abc"
    runs.mkdir(parents=True)
    pr_review.parent.mkdir(parents=True)
    pr_review.write_text("db\n", encoding="utf-8")
    (pr_review.parent / "engine.sqlite3-wal").write_text("wal\n", encoding="utf-8")
    config.write_text("config\n", encoding="utf-8")
    artifacts.mkdir(parents=True)
    (runs / "state.json").write_text("{}", encoding="utf-8")

    result = run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        state_root=state_root,
    )
    assert not runs.exists()
    assert not (state_root / "pr-review-v2").exists()
    assert config.is_file()
    assert artifacts.is_dir()
    assert len(result.deleted_paths) == 2


def test_cutover_preserves_wal_shm_sibling_paths(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    runs = state_root / "runs"
    pr_review = state_root / "pr-review-v2"
    runs.mkdir(parents=True)
    pr_review.mkdir()
    (runs / "nested.wal").write_text("inside\n", encoding="utf-8")
    (pr_review / "engine.sqlite3-shm").write_text("inside-shm\n", encoding="utf-8")
    sibling_wal = state_root / "runs-wal"
    sibling_shm = state_root / "pr-review-v2-shm"
    sibling_wal.write_text("keep-wal\n", encoding="utf-8")
    sibling_shm.write_text("keep-shm\n", encoding="utf-8")

    run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        state_root=state_root,
    )

    assert not runs.exists()
    assert not pr_review.exists()
    assert sibling_wal.read_text(encoding="utf-8") == "keep-wal\n"
    assert sibling_shm.read_text(encoding="utf-8") == "keep-shm\n"


def test_cutover_idempotent_when_paths_absent(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    first = run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        state_root=state_root,
    )
    second = run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        state_root=state_root,
    )
    assert first.deleted_paths == ()
    assert second.deleted_paths == ()
    assert first.already_absent
    assert second.already_absent


def test_cutover_rejects_symlink_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    state_root.mkdir()
    (state_root / "runs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError, match="symlink"):
        resolve_legacy_target(state_root, "runs")


def test_cutover_rejects_symlink_state_root_preserves_outside_data(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    preserved = outside / "runs" / "run-1"
    preserved.mkdir(parents=True)
    marker = preserved / "keep.txt"
    marker.write_text("secret\n", encoding="utf-8")
    state_link = tmp_path / "state-link"
    state_link.symlink_to(outside)
    with pytest.raises(ValidationError, match="symlink"):
        run_cutover_cleanup(
            confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
            state_root=state_link,
        )
    assert marker.read_text(encoding="utf-8") == "secret\n"


def test_validate_state_root_rejects_symlink_before_resolve(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    state_link = tmp_path / "state-link"
    state_link.symlink_to(outside)
    with pytest.raises(ValidationError, match="symlink"):
        validate_state_root(state_link)


def test_validate_state_root_rejects_symlinked_xdg_ancestor_preserves_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    real_root = outside / "ai_dev_loop"
    preserved = real_root / "runs" / "run-1"
    preserved.mkdir(parents=True)
    marker = preserved / "keep.txt"
    marker.write_text("secret\n", encoding="utf-8")
    state_home_link = tmp_path / "xdg-state-home"
    state_home_link.symlink_to(outside)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home_link))
    monkeypatch.setattr(
        "ai_dev_loop.paths.state_dir",
        lambda: state_home_link / "ai_dev_loop",
    )
    with pytest.raises(ValidationError, match="symlink"):
        validate_state_root(None)
    assert marker.read_text(encoding="utf-8") == "secret\n"


def test_cutover_rejects_active_scheduler_work(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    db_path = state_root / "engine.sqlite3"
    store = _init_scheduler_db(db_path)
    now = datetime.now(UTC)
    with store.begin_immediate() as conn:
        store.acquire_global_tick_lease(conn, owner_id="tick-1", now=now, ttl_seconds=60)
    (state_root / "runs").mkdir()
    with pytest.raises(ValidationError, match="active scheduler tick lease"):
        run_cutover_cleanup(
            confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
            state_root=state_root,
            now=now,
        )


def _tick_service(db_path: Path, artifact_root: Path) -> TickService:
    return TickService(
        SqliteSchedulerStore(db_path),
        ProtectedArtifactStore(artifact_root),
        BoundedGitAdmissionPort(),
    )


def test_tick_cannot_acquire_lease_while_cutover_holds_it(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    engine_path = state_root / "engine.sqlite3"
    (state_root / "runs").mkdir()
    now = datetime.now(UTC)
    with _cutover_coordination(state_root, engine_path, now):
        receipt = _tick_service(engine_path, state_root / "artifacts").run_once()
        assert receipt.lease_acquired is False


def test_cutover_without_existing_db_blocks_competing_tick(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "runs").mkdir()
    engine_path = state_root / "engine.sqlite3"
    assert not engine_path.exists()
    now = datetime.now(UTC)
    with _cutover_coordination(state_root, engine_path, now):
        assert engine_path.is_file()
        receipt = _tick_service(engine_path, state_root / "artifacts").run_once()
        assert receipt.lease_acquired is False
        lease_owner = SqliteSchedulerStore(engine_path)
        with lease_owner.begin_immediate() as conn:
            row = lease_owner.get_tick_lease_row(conn)
            assert row["owner_id"] == CUTOVER_TICK_OWNER
            assert row["status"] == "active"


def test_cutover_ignores_unrelated_default_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_root = tmp_path / "cleanup-root"
    cleanup_root.mkdir()
    global_root = tmp_path / "global-root"
    global_root.mkdir()
    global_db = global_root / "engine.sqlite3"
    cleanup_db = cleanup_root / "engine.sqlite3"
    global_store = SqliteSchedulerStore(global_db)
    cleanup_store = SqliteSchedulerStore(cleanup_db)
    now = datetime.now(UTC)
    (cleanup_root / "runs").mkdir()

    with global_store.begin_immediate() as conn:
        global_store.acquire_global_tick_lease(
            conn, owner_id="global-tick", now=now, ttl_seconds=60
        )

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.infrastructure.paths.default_engine_db_path",
        lambda: global_db,
    )
    result = run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        state_root=cleanup_root,
        now=now,
    )
    assert result.deleted_paths

    with global_store.begin_immediate() as conn:
        row = global_store.get_tick_lease_row(conn)
        assert row["owner_id"] == "global-tick"
        assert row["status"] == "active"

    with cleanup_store.begin_immediate() as conn:
        cleanup_store.acquire_global_tick_lease(
            conn, owner_id="cleanup-tick", now=now, ttl_seconds=60
        )
    with pytest.raises(ValidationError, match="active scheduler tick lease"):
        run_cutover_cleanup(
            confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
            state_root=cleanup_root,
            now=now,
        )


def test_cutover_refuses_concurrent_cleanup(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "runs").mkdir()
    now = datetime.now(UTC)
    lock = FileLock(_cutover_lock_path(state_root))
    lock.acquire(
        LockMetadata(
            pid=os.getpid(),
            run_id="other-cutover",
            repository_path=str(state_root),
            started_at=now,
        )
    )
    try:
        with pytest.raises(ValidationError, match="concurrent cutover cleanup"):
            run_cutover_cleanup(
                confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
                state_root=state_root,
            )
    finally:
        lock.release()


def test_scheduler_active_work_reason_none_without_db(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    assert (
        scheduler_active_work_reason(
            resolved_state_root=state_root,
            engine_path=state_root / "missing.sqlite3",
        )
        is None
    )


def test_cutover_dry_run_reports_paths_without_deleting(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    runs = state_root / "runs"
    runs.mkdir(parents=True)
    (runs / "marker.txt").write_text("keep\n", encoding="utf-8")
    result = run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        dry_run=True,
        state_root=state_root,
    )
    assert runs.is_dir()
    assert result.dry_run is True
    assert str(runs) in result.deleted_paths


def test_competing_tick_blocked_when_deletion_outlasts_lease_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "runs").mkdir()
    (state_root / "pr-review-v2").mkdir()
    engine_path = state_root / "engine.sqlite3"
    delete_calls = 0

    def slow_first_delete(path: Path) -> None:
        nonlocal delete_calls
        delete_calls += 1
        if delete_calls == 1:
            store = SqliteSchedulerStore(engine_path)
            with store.begin_immediate() as conn:
                lease_row = store.get_tick_lease_row(conn)
            assert lease_row["owner_id"] == CUTOVER_TICK_OWNER
            assert lease_row["expires_at"] is not None
            lease_expires_at = parse_utc_instant(str(lease_row["expires_at"]))
            competing_tick_time = lease_expires_at + timedelta(seconds=1)

            assert cutover_cleanup_blocks_scheduler_tick(state_root) is True

            tick = TickService(
                store,
                ProtectedArtifactStore(state_root / "artifacts"),
                BoundedGitAdmissionPort(),
                now_factory=lambda: competing_tick_time,
            )
            blocked_receipt = tick.run_once()
            assert blocked_receipt.lease_acquired is False

            with store.begin_immediate() as conn:
                still_held = store.get_tick_lease_row(conn)
            assert still_held["owner_id"] == CUTOVER_TICK_OWNER
            assert parse_utc_instant(str(still_held["expires_at"])) < competing_tick_time

            with patch(
                "ai_dev_loop.scheduler.application.cutover_cleanup.cutover_cleanup_blocks_scheduler_tick",
                return_value=False,
            ):
                unguarded_receipt = tick.run_once()
            assert unguarded_receipt.lease_acquired is True
        _delete_legacy_tree(path)

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.cutover_cleanup._delete_legacy_tree",
        slow_first_delete,
    )
    run_cutover_cleanup(
        confirmation_token=CUTOVER_CONFIRMATION_TOKEN,
        state_root=state_root,
    )
    assert delete_calls == 2

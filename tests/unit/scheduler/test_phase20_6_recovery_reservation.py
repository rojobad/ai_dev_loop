"""Reservation acquisition tests for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import _blocked_recovery_fixture

from ai_dev_loop.scheduler.application.contracts import ReservationStatus
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.application.recovery_start import RecoveryStartService
from ai_dev_loop.scheduler.application.recovery_worktree import ProductionRecoveryWorktreePort


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_recovery_start_reacquires_released_blocked_source_reservation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    with store.begin_read() as conn:
        reservation = conn.execute(
            """
            SELECT status FROM scheduler_repository_reservations
            WHERE run_id = ?
            """,
            (source_run_id,),
        ).fetchone()
        assert reservation is not None
        assert str(reservation[0]) == ReservationStatus.RELEASED.value

    prepared = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="recovery reservation test commit",
    )
    started = RecoveryStartService(
        store,
        artifacts,
        worktree_port=ProductionRecoveryWorktreePort(),
    ).start(prepared.recovery_id)
    assert started.changed is True

    with store.begin_read() as conn:
        reservation = store.get_reservation_for_run(conn, source_run_id)
        assert reservation is not None
        assert str(reservation["status"]) == ReservationStatus.ACTIVE.value
        assert str(reservation["run_id"]) == source_run_id
        active = store.get_active_reservation(conn, str(reservation["worktree_key"]))
        assert active is not None
        assert str(active["run_id"]) == source_run_id

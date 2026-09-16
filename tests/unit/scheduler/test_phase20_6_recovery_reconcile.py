"""Reconcile and abort deferral tests for Phase 20.6."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.recovery_abort import RecoveryAbortService
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.application.recovery_reconcile import RecoveryReconcileService
from ai_dev_loop.scheduler.application.recovery_start import RecoveryStartService
from ai_dev_loop.scheduler.application.recovery_worktree import FakeRecoveryWorktreePort
from ai_dev_loop.scheduler.domain.recovery import (
    ABORT_PENDING_RECOVERY_STATE_KIND,
    ABORTED_RECOVERY_STATE_KIND,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_abort_active_recovery_defers_run_abort_outside_collect(
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
    prepared = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="abort defer commit",
    )
    fake_worktree = FakeRecoveryWorktreePort()
    RecoveryStartService(store, artifacts, worktree_port=fake_worktree).start(prepared.recovery_id)
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert row is not None
    assert row["recovery_run_id"]
    RecoveryAbortService(store, artifacts).abort(prepared.recovery_id)
    reconcile = RecoveryReconcileService(
        store,
        artifacts,
        abort_backend=FakeAgentProcessBackend(),
    )
    with store.begin_read() as conn:
        receipts = reconcile.reconcile_pending_recoveries(conn)
    assert any(r.action == "recovery_abort_deferred" for r in receipts)
    finalize = reconcile.finalize_deferred_aborts()
    assert finalize
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert str(row["state_kind"]) in {
        ABORTED_RECOVERY_STATE_KIND,
        ABORT_PENDING_RECOVERY_STATE_KIND,
    }

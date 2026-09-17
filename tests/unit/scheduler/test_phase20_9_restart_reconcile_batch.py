"""Restart reconcile batch isolation for Phase 20.9."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.sequence_restart_reconcile import (
    SequenceRestartReconcileService,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FIXED_NOW = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_reconcile_pending_restart_work_continues_after_invalid_terminal_leaf(
    scheduler_paths: dict[str, Path],
) -> None:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = SequenceRestartReconcileService(
        store,
        artifacts,
        handoff=MagicMock(),
        now_factory=lambda: FIXED_NOW,
    )
    calls: list[str] = []

    def fake_terminal(conn: sqlite3.Connection, run_id: str) -> TickRunReceipt:
        calls.append(run_id)
        if run_id == "terminal-invalid":
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        return TickRunReceipt(run_id=run_id, action="sequence_finalization_reconciled")

    service.reconcile_terminal_current_leaf = fake_terminal  # type: ignore[method-assign]
    store.list_sequence_terminal_current_run_ids = (  # type: ignore[method-assign]
        lambda _conn: ["terminal-invalid", "terminal-ok"]
    )
    store.list_unpublished_sequence_execution_replacement_intents = (  # type: ignore[method-assign]
        lambda _conn: []
    )
    store.list_pending_sequence_review_recovery_run_ids = (  # type: ignore[method-assign]
        lambda _conn: []
    )
    with store.begin_immediate() as conn:
        receipts = service.reconcile_pending_restart_work(conn)
    assert calls == ["terminal-invalid", "terminal-ok"]
    assert any(receipt.action == "checkpoint_result_invalid" for receipt in receipts)
    assert any(receipt.action == "sequence_finalization_reconciled" for receipt in receipts)

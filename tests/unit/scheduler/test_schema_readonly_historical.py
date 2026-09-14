"""Read-only compatibility tests for historical scheduler schema versions."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.scheduler.helpers import sample_submitted_state
from tests.unit.scheduler.test_v5_migration import _pause_v4_database
from tests.unit.scheduler.test_v6_migration import _pause_v5_database, _pause_v6_database

from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.domain.events import RunAbortedEvent, RunSubmittedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_open_readonly_accepts_v4_database_without_migrating(tmp_path: Path) -> None:
    db = _pause_v4_database(tmp_path)
    readonly = SqliteSchedulerStore.open_readonly(db)
    assert _user_version(db) == 4
    with readonly.begin_read() as conn:
        assert readonly.schema_supports_checkpoint_holds(conn) is False
        assert readonly.has_checkpoint_reconciliation_hold(conn, "missing-run") is False
        assert readonly.has_unresolved_abort_hold(conn, "missing-run") is False


def test_open_readonly_accepts_v5_database_without_migrating(tmp_path: Path) -> None:
    db = _pause_v5_database(tmp_path)
    assert _user_version(db) == 5
    readonly = SqliteSchedulerStore.open_readonly(db)
    with readonly.begin_read() as conn:
        assert readonly.schema_supports_checkpoint_holds(conn) is False
        assert readonly.has_unresolved_abort_hold(conn, "missing-run") is False


def test_open_readonly_accepts_v6_database_without_migrating(tmp_path: Path) -> None:
    db = _pause_v6_database(tmp_path)
    assert _user_version(db) == 6
    readonly = SqliteSchedulerStore.open_readonly(db)
    with readonly.begin_read() as conn:
        assert readonly.schema_supports_checkpoint_holds(conn) is False


def test_aborted_run_safe_action_on_v6_readonly_database_does_not_query_checkpoint_holds(
    tmp_path: Path,
) -> None:
    db = _pause_v6_database(tmp_path)
    store = SqliteSchedulerStore(db, bootstrap=False)
    state = sample_submitted_state(run_id="aborted-v6-run")
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=RunSubmittedEvent(
                run_id=state.run_id,
                idempotency_key=state.idempotency_key,
                worktree_key=state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=now,
        )
        aborted = apply_run_aborted(
            state,
            RunAbortedEvent(
                run_id=state.run_id,
                reason="user_requested_abort",
                prior_state_kind="submitted",
            ),
            now_text=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        store.compare_and_swap_state(
            conn,
            run_id=state.run_id,
            expected_version=1,
            new_state=aborted,
            now=now,
        )
    readonly = SqliteSchedulerStore.open_readonly(db)
    with readonly.begin_read() as conn:
        loaded, _, _ = readonly.load_validated_snapshot(conn, state.run_id)
        action = safe_next_action_for_scheduler_state(readonly, conn, loaded)
        assert action.kind in {"none", "scheduler_tick"}

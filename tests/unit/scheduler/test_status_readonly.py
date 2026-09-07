"""Unit tests for read-only scheduler status/list projections."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tests.unit.scheduler.helpers import sample_submitted_state

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def test_list_and_status_leave_empty_xdg_state_unchanged(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    assert not state_root.exists()

    listings = scheduler_list()
    assert listings == []

    with pytest.raises(SchedulerEngineError, match="scheduler database not found"):
        scheduler_status("missing-run-id")

    assert not state_root.exists()


def test_open_readonly_uses_read_only_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db_path)
    state = sample_submitted_state(run_id="fixture-run")
    with store.begin_immediate() as conn:
        from datetime import UTC, datetime

        from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent

        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-test",
            event=RunSubmittedEvent(
                run_id=state.run_id,
                idempotency_key=state.idempotency_key,
                worktree_key=state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=datetime.now(tz=UTC),
        )

    readonly = SqliteSchedulerStore.open_readonly(db_path)
    assert readonly._read_only is True

    with patch.object(readonly, "_apply_database_permissions") as chmod_mock:
        with readonly.begin_read() as conn:
            count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        assert count == 1
        chmod_mock.assert_not_called()

    with readonly.begin_read() as conn:
        rows = readonly.list_runs(conn)
    assert len(rows) == 1

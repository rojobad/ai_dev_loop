"""Integration tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture

from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
from ai_dev_loop.scheduler.application.rollover_worktree import FakeRolloverWorktreePort
from ai_dev_loop.scheduler.domain.rollover import (
    ACTIVE_ROLLOVER_STATE_KIND,
    PREPARED_ROLLOVER_STATE_KIND,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    FreshCodexReviewerBinding,
    MaxIterationsReachedState,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepare_from_maxed_source_is_process_free(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_run_id, store, artifacts, _ = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    service = RolloverPrepareService(store, artifacts)
    first = service.prepare(source_run_id, commit_message="integration rollover commit")
    second = service.prepare(source_run_id, commit_message="integration rollover commit")
    assert first.state_kind == PREPARED_ROLLOVER_STATE_KIND
    assert first.changed is True
    assert second.idempotent_replay is True
    assert second.rollover_id == first.rollover_id


def test_standalone_rollover_prepare_start_fresh_reviewer_lifecycle(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_run_id, store, artifacts, _ = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    with store.begin_read() as conn:
        source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
    assert isinstance(source_state, MaxIterationsReachedState)
    source_session = source_state.codex.reviewer_session_id
    assert source_session

    prepared = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="integration lifecycle commit",
    )
    started = RolloverStartService(
        store,
        artifacts,
        worktree_port=FakeRolloverWorktreePort(),
    ).start(prepared.rollover_id)
    assert started.state_kind == ACTIVE_ROLLOVER_STATE_KIND
    assert started.changed is True

    rollover_run_id = started.rollover_run_id
    with store.begin_read() as conn:
        rollover_state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
    assert isinstance(rollover_state, AwaitingCodexReviewState)
    assert isinstance(rollover_state.context.codex, FreshCodexReviewerBinding)
    assert rollover_state.codex.reviewer_session_id is None
    assert rollover_state.fresh_rollover is not None
    assert rollover_state.fresh_rollover.source_run_id == source_run_id
    assert rollover_state.cursor.chat_id is None

"""Start tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture

from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
from ai_dev_loop.scheduler.application.rollover_worktree import FakeRolloverWorktreePort
from ai_dev_loop.scheduler.domain.rollover import ACTIVE_ROLLOVER_STATE_KIND
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    FreshCodexReviewerBinding,
    MaxIterationsReachedState,
)


def test_start_materializes_fresh_reviewer_without_source_session(
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
        commit_message="fresh reviewer rollover commit",
    )
    started = RolloverStartService(
        store,
        artifacts,
        worktree_port=FakeRolloverWorktreePort(),
    ).start(prepared.rollover_id)
    assert started.changed is True
    assert started.state_kind == ACTIVE_ROLLOVER_STATE_KIND

    rollover_run_id = started.rollover_run_id
    with store.begin_read() as conn:
        rollover_state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
    assert isinstance(rollover_state, AwaitingCodexReviewState)
    assert isinstance(rollover_state.context.codex, FreshCodexReviewerBinding)
    assert rollover_state.codex.reviewer_session_id is None
    assert rollover_state.fresh_rollover is not None
    assert rollover_state.fresh_rollover.source_run_id == source_run_id
    assert rollover_state.cursor.chat_id is None


def test_start_is_idempotent_for_active_rollover(
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
    prepared = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="idempotent start commit",
    )
    service = RolloverStartService(
        store,
        artifacts,
        worktree_port=FakeRolloverWorktreePort(),
    )
    first = service.start(prepared.rollover_id)
    second = service.start(prepared.rollover_id)
    assert first.rollover_run_id == second.rollover_run_id
    assert second.idempotent_replay is True
    assert second.changed is False

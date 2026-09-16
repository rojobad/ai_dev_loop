"""Start tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
from ai_dev_loop.scheduler.application.rollover_worktree import FakeRolloverWorktreePort
from ai_dev_loop.scheduler.domain.rollover import ACTIVE_ROLLOVER_STATE_KIND
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
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
    assert rollover_state.context.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED
    assert rollover_state.context.sequence is None


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


def test_competing_rollover_definitions_reject_second_live_authority(
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
    prepare = RolloverPrepareService(store, artifacts)
    first_prepared = prepare.prepare(
        source_run_id,
        commit_message="first competing rollover commit",
    )
    with pytest.raises(SchedulerEngineError, match="live rollover authority"):
        prepare.prepare(
            source_run_id,
            commit_message="second competing rollover commit",
        )
    service = RolloverStartService(
        store,
        artifacts,
        worktree_port=FakeRolloverWorktreePort(),
    )
    service.start(first_prepared.rollover_id)


def test_conclusive_rollover_abort_releases_source_target_reservation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import itertools
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort

    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )
    from ai_dev_loop.scheduler.application.rollover_abort import RolloverAbortService
    from ai_dev_loop.scheduler.application.tick import TickService
    from ai_dev_loop.scheduler.domain.rollover import ABORTED_ROLLOVER_STATE_KIND

    source_run_id, store, artifacts, _ = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    prepared = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="abort reservation release commit",
    )
    RolloverStartService(
        store,
        artifacts,
        worktree_port=FakeRolloverWorktreePort(),
    ).start(prepared.rollover_id)
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, source_run_id) is not None
    RolloverAbortService(store, artifacts).abort(prepared.rollover_id)
    counter = itertools.count()
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 15, 17, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-rollover-abort-{next(counter)}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    for _ in range(20):
        tick.run_once()
        with store.begin_read() as conn:
            row = store.get_authenticated_rollover(conn, prepared.rollover_id)
            if row is not None and str(row["state_kind"]) == ABORTED_ROLLOVER_STATE_KIND:
                break
    with store.begin_read() as conn:
        row = store.get_authenticated_rollover(conn, prepared.rollover_id)
        assert row is not None
        assert str(row["state_kind"]) == ABORTED_ROLLOVER_STATE_KIND
        assert store.get_reservation_for_run(conn, source_run_id) is None

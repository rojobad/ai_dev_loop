"""Fresh-rollover production flow tests for Phase 20.6.5."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.fresh_agent_successor import (
    fresh_agent_successor_workflow_limits,
)
from ai_dev_loop.scheduler.application.review_budget import effective_review_ceiling_for_run
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
from ai_dev_loop.scheduler.application.rollover_worktree import ProductionRolloverWorktreePort
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    CompletedState,
    MaxIterationsReachedState,
    WaitingForCursorFixState,
)

_ATTEMPT_COUNTER = itertools.count()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_fresh_rollover_successor_budget_resets_review_numbering(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_run_id, store, artifacts, fixed_now = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
        max_reviews=2,
        review_sequence="findings,findings",
    )
    with store.begin_read() as conn:
        before_extend, _, _ = store.load_validated_snapshot(conn, source_run_id)
        assert isinstance(before_extend, MaxIterationsReachedState)
        assert before_extend.codex.reviews_completed == 2
    ReviewBudgetExtendService(
        store,
        artifacts,
        now_factory=lambda: fixed_now,
    ).extend(source_run_id, target_total=5)
    with store.begin_read() as conn:
        source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        workflow = fresh_agent_successor_workflow_limits(store, conn, source_state)
        assert workflow.max_review_iterations == 5
        assert effective_review_ceiling_for_run(store, conn, source_state) == 5


def test_fresh_rollover_review_findings_lazy_chat_correction_and_accepted_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    source_run_id, store, artifacts, _ = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
        max_reviews=3,
        review_sequence="findings,findings,findings",
    )
    prepared = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="rollover production flow commit",
    )
    started = RolloverStartService(
        store,
        artifacts,
        worktree_port=ProductionRolloverWorktreePort(),
    ).start(prepared.rollover_id)
    rollover_run_id = started.rollover_run_id

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
    assert isinstance(state, AwaitingCodexReviewState)
    assert state.fresh_rollover is not None
    assert state.codex.review_iteration == 1
    assert state.codex.reviews_completed == 0
    assert state.context.workflow.max_review_iterations == 3
    assert state.cursor.chat_id is None

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    rollover_counter = itertools.count(2_000)
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 15, 16, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-rollover-flow-{next(rollover_counter)}",
        attempt_id_factory=lambda: f"att-{next(rollover_counter):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
            if state.kind == "waiting_for_cursor_fix":
                break
            if state.kind == "blocked":
                raise AssertionError(
                    f"rollover run blocked: {state.block_reason_kind}: {state.block_reason_summary}"
                )
    else:
        raise AssertionError(
            f"rollover run did not reach waiting_for_cursor_fix (last kind={state.kind})"
        )

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
        assert isinstance(state, WaitingForCursorFixState)
        assert state.fresh_rollover is not None
        assert state.cursor.chat_id is None
        assert state.cursor.iteration == 2
        create_chat = conn.execute(
            """
            SELECT effect_kind FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ? AND status = 'pending'
            """,
            (rollover_run_id, CREATE_CHAT_EFFECT_KIND),
        ).fetchone()
    assert create_chat is not None

    for _ in range(30):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
            if state.cursor.chat_id:
                break
    assert state.cursor.chat_id

    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    for _ in range(120):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
            if state.kind == "completed":
                break
            if state.kind == "blocked":
                raise AssertionError(
                    f"rollover run blocked after correction: "
                    f"{state.block_reason_kind}: {state.block_reason_summary}"
                )
    else:
        raise AssertionError(
            f"rollover run did not reach completed after correction (last kind={state.kind})"
        )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
    assert isinstance(state, CompletedState)
    assert state.codex.reviews_completed == 2
    with store.begin_read() as conn:
        correction = conn.execute(
            """
            SELECT effect_kind FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (rollover_run_id, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchone()
    assert correction is not None

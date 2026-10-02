"""Sequence-aware Phase 22 routing auto-retry acceptance."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_RUN_IDS
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
from tests.unit.scheduler.test_phase22_codex_routing_auto_retry import (
    _authorize_due_routing_retry,
    _run_until,
    _tick_service,
)

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.codex_routing_policy import ROUTING_AUTO_RETRY_DELAY_SECONDS
from ai_dev_loop.scheduler.domain.sequence import (
    AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_sequence_non_final_phase_routing_auto_retry_preserves_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, start.run_id, target_kind="waiting_codex_review_retry", max_ticks=120)
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    due_tick.run_once()
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, start.run_id)
        assert state.kind == "awaiting_codex_review"
        sequence = due_tick.store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
        assert sequence.current_run_id == start.run_id


def test_sequence_abort_during_routing_wait_blocks_auto_authorization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, start.run_id, target_kind="waiting_codex_review_retry", max_ticks=120)
    SequenceAbortService(tick.store, now_factory=lambda: failure_time).abort_sequence(sequence_id)
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    due_tick.run_once()
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, start.run_id)
        assert state.kind == "aborted"


def test_sequence_stale_leaf_blocks_routing_auto_authorization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, start.run_id, target_kind="waiting_codex_review_retry", max_ticks=120)
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    with due_tick.store.begin_read() as conn:
        sequence = due_tick.store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
        stale_leaf = sequence.model_copy(update={"current_run_id": "stale-sequence-leaf-run"})
    original_load = due_tick.store.load_sequence_state_only

    def _stale_sequence_only(conn: object, sid: str) -> ActiveSequenceState:
        if sid == sequence_id:
            return stale_leaf
        return original_load(conn, sid)  # type: ignore[arg-type]

    with patch.object(due_tick.store, "load_sequence_state_only", _stale_sequence_only):
        receipt = due_tick.run_once()
    assert not any(
        item.action == "codex_routing_auto_retry_authorized" for item in receipt.run_receipts
    )
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, start.run_id)
        assert state.kind == "waiting_codex_review_retry"
        assert state.codex.routing_auto_retry_authorizations_used == 0


def test_sequence_non_final_routing_retry_completes_checkpoint_handoff(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, start.run_id, target_kind="waiting_codex_review_retry", max_ticks=120)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    due_tick = _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        start.run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    _run_until(due_tick, start.run_id, target_kind="completed", max_ticks=160)
    phase_two_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 60),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    for _ in range(80):
        phase_two_tick.run_once()
    sequence_status = SequenceStatusService(phase_two_tick.store).get_status(sequence_id)
    assert sequence_status.current_run_id == FIXED_RUN_IDS[1]
    assert sequence_status.current_ordinal == 2
    log = subprocess.check_output(["git", "log", "--oneline"], cwd=git_repo, text=True)
    assert "checkpoint after phase one" in log


def test_sequence_final_phase_routing_retry_completes_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    phase_one_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(phase_one_tick, start.run_id, target_kind="completed", max_ticks=160)
    phase_two_id = SequenceStatusService(phase_one_tick.store).get_status(sequence_id).current_run_id
    assert phase_two_id == FIXED_RUN_IDS[1]
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    routing_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(minutes=10),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    for _ in range(240):
        routing_tick.run_once()
        with routing_tick.store.begin_read() as conn:
            state, _, _ = routing_tick.store.load_validated_snapshot(conn, phase_two_id)
            if state.kind == "waiting_codex_review_retry":
                break
    else:
        with routing_tick.store.begin_read() as conn:
            state, _, _ = routing_tick.store.load_validated_snapshot(conn, phase_two_id)
        raise AssertionError(
            f"phase two did not reach routing retry wait (last kind={state.kind})"
        )
    with routing_tick.store.begin_read() as conn:
        phase_two, _, _ = routing_tick.store.load_validated_snapshot(conn, phase_two_id)
        reviewer = phase_two.codex.reviewer_session_id
        cursor_iterations = phase_two.cursor.iteration
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    complete_tick = _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        phase_two_id,
        failure_time=failure_time + timedelta(minutes=10),
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    _run_until(complete_tick, phase_two_id, target_kind="completed", max_ticks=160)
    status = SequenceStatusService(complete_tick.store).get_status(sequence_id)
    assert status.state_kind == AWAITING_FINALIZATION_SEQUENCE_STATE_KIND
    assert status.current_run_id == FIXED_RUN_IDS[1]
    with complete_tick.store.begin_read() as conn:
        final_state, _, _ = complete_tick.store.load_validated_snapshot(conn, phase_two_id)
        assert final_state.codex.reviewer_session_id == reviewer
        assert final_state.codex.reviews_completed == 1
        assert final_state.cursor.iteration == cursor_iterations
        staged = subprocess.check_output(
            ["git", "diff", "--cached", "--name-only"],
            cwd=git_repo,
            text=True,
        )
        assert staged.strip()

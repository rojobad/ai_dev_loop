"""Unit tests for scheduler abort, cancellation fences, and capacity release."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.scheduler.test_tick import (
    FakeGitAdmissionPort,
    _bootstrap_run,
    _claim_ids,
)

from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.contracts import AbortProcessAction
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
from ai_dev_loop.scheduler.domain.state import AuthorizedState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_CANCELLED,
    EFFECT_STATUS_CANCELLED,
    TIMER_STATUS_CANCELLED,
    SqliteSchedulerStore,
)


def _abort_service(
    store: SqliteSchedulerStore,
    backend: FakeAgentProcessBackend,
) -> SchedulerAbortService:
    return SchedulerAbortService(
        store,
        backend,
        now_factory=lambda: datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
        fence_id_factory=lambda: "fnc-abort-test",
    )


def test_apply_run_aborted_from_authorized(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, AuthorizedState)
    from ai_dev_loop.scheduler.domain.events import RunAbortedEvent

    aborted = apply_run_aborted(
        state,
        RunAbortedEvent(
            run_id=run_id, reason="user_requested_abort", prior_state_kind="authorized"
        ),
        now_text="2026-09-09T12:00:00.000000Z",
    )
    assert aborted.kind == "aborted"
    assert aborted.prior_state_kind == "authorized"


def test_abort_before_launch_cancels_pending_work(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    backend = FakeAgentProcessBackend()
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.abort_persisted is True
    assert result.process_action is AbortProcessAction.NONE
    assert result.termination_pending is False
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        effect = conn.execute(
            "SELECT status FROM scheduler_effects WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert effect is not None
        assert str(effect[0]) == EFFECT_STATUS_CANCELLED
        assert store.get_capacity_row(conn)["holder_run_id"] is None
        assert store.get_reservation_for_run(conn, run_id) is None


def test_abort_idempotent_replay(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    backend = FakeAgentProcessBackend()
    service = _abort_service(store, backend)
    first = service.abort_run(run_id)
    second = service.abort_run(run_id)
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert backend.terminate_calls == []


def test_abort_active_attempt_terminates_owned_unit(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, exit_code=0)
    )
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        now_factory=lambda: datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-abort",
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
        claim_id_factory=_claim_ids(),
        attempt_id_factory=lambda: "att-" + "b" * 32,
        attempt_backend=backend,
        lease_ttl_seconds=60,
    )
    receipt = tick.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_active"} for item in receipt.run_receipts
    )
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.process_action is AbortProcessAction.TERMINATED
    assert backend.terminate_calls
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is None
        row = conn.execute(
            "SELECT status FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert str(row[0]) == ATTEMPT_STATUS_CANCELLED
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_abort_cancels_pending_usage_limit_timer(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    with store.begin_read() as conn:
        source = conn.execute(
            "SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence ASC LIMIT 1",
            (run_id,),
        ).fetchone()
        assert source is not None
    with store.begin_immediate() as conn:
        store.insert_retry_timer(
            conn,
            timer_id="timer-abort",
            source_event_id=str(source[0]),
            run_id=run_id,
            due_at=now,
            target_effect_id="effect-retry",
            expected_run_version=2,
            now=now,
        )
    _abort_service(store, FakeAgentProcessBackend()).abort_run(run_id)
    with store.begin_read() as conn:
        timer = conn.execute(
            "SELECT status FROM scheduler_timers WHERE timer_id = ?",
            ("timer-abort",),
        ).fetchone()
        assert timer is not None
        assert str(timer[0]) == TIMER_STATUS_CANCELLED

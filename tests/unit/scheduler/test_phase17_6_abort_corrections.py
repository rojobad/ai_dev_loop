"""Regression tests for Phase 17.6 Codex abort/recovery corrections."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import (
    FakeGitAdmissionPort,
    _bootstrap_run,
    _claim_ids,
)

from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.contracts import (
    AbortProcessAction,
    SafeNextActionKind,
)
from ai_dev_loop.scheduler.application.controller_read import (
    find_scheduler_candidates,
    load_scheduler_candidate,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.events import RunAbortedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
from ai_dev_loop.scheduler.domain.state import AbortedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_CANCELLED,
    SqliteSchedulerStore,
)


def _assert_abort_resources_released(store: SqliteSchedulerStore, run_id: str) -> None:
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] is None
        assert store.get_reservation_for_run(conn, run_id) is None


def _persist_abort_cancellation_without_cleanup(
    store: SqliteSchedulerStore,
    run_id: str,
    *,
    fence_id: str = "fnc-crash-sim",
) -> None:
    """Simulate a crash after durable cancellation but before post-commit cleanup."""
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, AbortedState):
            aborted = apply_run_aborted(
                state,
                RunAbortedEvent(
                    run_id=run_id,
                    reason="user_requested_abort",
                    prior_state_kind=state.kind,
                ),
                now_text="2026-09-09T12:00:00.000000Z",
            )
            store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=aborted,
                now=now,
            )
        store.cancel_live_work(conn, run_id=run_id, now=now)
        store.cancel_nonterminal_attempts(
            conn,
            run_id=run_id,
            completion_fence_id=fence_id,
            now=now,
        )


def _fresh_tick(
    tmp_path: Path,
    store: SqliteSchedulerStore,
    *,
    backend: FakeAgentProcessBackend | None = None,
    repo_root: str | None = None,
) -> TickService:
    root = repo_root or str(tmp_path / "repo")
    return TickService(
        store,
        ProtectedArtifactStore(tmp_path / "artifacts"),
        FakeGitAdmissionPort(resolved_root=root),
        now_factory=lambda: datetime(2026, 9, 9, 12, 30, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-fresh-{secrets.token_hex(4)}",
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
        claim_id_factory=_claim_ids(),
        attempt_backend=backend or FakeAgentProcessBackend(),
        lease_ttl_seconds=60,
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


def _launch_active_attempt(
    tmp_path: Path,
    *,
    backend: FakeAgentProcessBackend,
    attempt_id: str = "att-" + "b" * 32,
    scenario: FakeAttemptScenario | None = None,
) -> tuple[SqliteSchedulerStore, str, TickService]:
    if scenario is not None:
        backend.set_scenario(attempt_id, scenario)
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        now_factory=lambda: datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-abort-corr",
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
        claim_id_factory=_claim_ids(),
        attempt_id_factory=lambda: attempt_id,
        attempt_backend=backend,
        lease_ttl_seconds=60,
    )
    receipt = tick.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_active"} for item in receipt.run_receipts
    )
    return store, run_id, tick


def test_abort_successful_stop_releases_capacity_after_unit_inactive(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, exit_code=0)
    )
    store, run_id, _ = _launch_active_attempt(tmp_path, backend=backend)
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.process_action is AbortProcessAction.TERMINATED
    assert result.termination_pending is False
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] is None


def test_abort_refused_keeps_capacity_until_unit_stops(tmp_path: Path) -> None:
    attempt_id = "att-" + "b" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, owned=True, exit_code=0)
    )
    store, run_id, _ = _launch_active_attempt(
        tmp_path,
        backend=backend,
        attempt_id=attempt_id,
    )
    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(
            active_ticks=2,
            owned=True,
            terminate_raises_value_error=True,
            exit_code=0,
        ),
    )
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.process_action is AbortProcessAction.REFUSED
    assert result.termination_pending is True
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id
        row = conn.execute(
            "SELECT ingested FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 0


def test_abort_unavailable_keeps_capacity(tmp_path: Path) -> None:
    attempt_id = "att-" + "c" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, owned=True, exit_code=0)
    )
    store, run_id, _ = _launch_active_attempt(
        tmp_path,
        backend=backend,
        attempt_id=attempt_id,
    )
    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=2, owned=True, observation_unavailable=True, exit_code=0),
    )
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.process_action is AbortProcessAction.UNAVAILABLE
    assert result.termination_pending is True
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id


def test_abort_timed_out_stop_keeps_capacity_until_inactive(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=2,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    store, run_id, _ = _launch_active_attempt(tmp_path, backend=backend)
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.process_action is AbortProcessAction.TERMINATED
    assert result.termination_pending is True
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id


def test_stale_tick_reconcile_does_not_release_cancelled_hold(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=2,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    store, run_id, _ = _launch_active_attempt(tmp_path, backend=backend)
    _abort_service(store, backend).abort_run(run_id)
    now = datetime(2026, 9, 9, 12, 30, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.reconcile_stale_tick_resources(
            conn,
            current_generation=999_999,
            now=now,
        )
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id


def test_unresolved_cancellation_blocks_conflicting_capacity_claim(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=2,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    store, run_id_a, _ = _launch_active_attempt(tmp_path, backend=backend)
    abort = _abort_service(store, backend).abort_run(run_id_a)
    assert abort.termination_pending is True
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id_a
        assert (
            store.try_acquire_capacity(
                conn,
                run_id="run-conflict",
                claim_id="claim-conflict",
                tick_owner_id="tick-conflict",
                tick_lease_generation=1,
                now=datetime(2026, 9, 9, 12, 1, tzinfo=UTC),
            )
            is False
        )


def test_repeated_abort_retries_pending_termination(tmp_path: Path) -> None:
    attempt_id = "att-" + "d" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=2,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    store, run_id, _ = _launch_active_attempt(tmp_path, backend=backend, attempt_id=attempt_id)
    service = _abort_service(store, backend)
    first = service.abort_run(run_id)
    assert first.termination_pending is True
    second = service.abort_run(run_id)
    assert second.termination_pending is True
    assert second.idempotent_replay is False
    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=0, exit_code=0),
    )
    third = service.abort_run(run_id)
    assert third.termination_pending is False
    assert third.idempotent_replay is True


def test_crash_after_cancellation_releases_resources_in_same_transaction(
    tmp_path: Path,
) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    service = _abort_service(store, FakeAgentProcessBackend())
    original_reconcile = SchedulerAbortService._reconcile_existing_abort

    def crash_before_reconcile(self, run_id_arg: str, **kwargs: object) -> object:
        _assert_abort_resources_released(self.store, run_id_arg)
        raise RuntimeError("simulated crash before reconcile")

    SchedulerAbortService._reconcile_existing_abort = crash_before_reconcile  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            service.abort_run(run_id)
    finally:
        SchedulerAbortService._reconcile_existing_abort = original_reconcile  # type: ignore[method-assign]
    _assert_abort_resources_released(store, run_id)


def test_crash_before_launch_recovered_by_fresh_tick(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path, repo_root=str(tmp_path / "repo"))
    _persist_abort_cancellation_without_cleanup(store, run_id)
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert run_id in store.list_tick_eligible_run_ids(conn)
    status = SchedulerStatusService(store).get_status(run_id)
    assert status.summary.safe_next_action.kind == SafeNextActionKind.SCHEDULER_TICK
    receipt = _fresh_tick(tmp_path, store, repo_root=str(tmp_path / "repo")).run_once()
    assert any(item.action == "abort_resource_cleanup" for item in receipt.run_receipts)
    assert not any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in receipt.run_receipts
    )
    _assert_abort_resources_released(store, run_id)


def test_crash_between_attempts_recovered_by_fresh_tick(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    store, run_id, tick = _launch_active_attempt(tmp_path, backend=backend)
    tick.run_once()
    _persist_abort_cancellation_without_cleanup(store, run_id)
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert run_id in store.list_tick_eligible_run_ids(conn)
    receipt = _fresh_tick(
        tmp_path,
        store,
        backend=backend,
        repo_root=str(tmp_path / "repo"),
    ).run_once()
    assert any(item.action == "abort_resource_cleanup" for item in receipt.run_receipts)
    assert not any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in receipt.run_receipts
    )
    _assert_abort_resources_released(store, run_id)


def test_crash_after_abort_persist_recovered_by_repeat_abort(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, exit_code=0)
    )
    store, run_id, _ = _launch_active_attempt(tmp_path, backend=backend)
    fence_id = "fnc-crash-test"
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        from ai_dev_loop.scheduler.domain.events import RunAbortedEvent
        from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted

        state, version, _ = store.load_validated_snapshot(conn, run_id)
        aborted = apply_run_aborted(
            state,
            RunAbortedEvent(
                run_id=run_id,
                reason="user_requested_abort",
                prior_state_kind=state.kind,
            ),
            now_text="2026-09-09T12:00:00.000000Z",
        )
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=aborted,
            now=now,
        )
        store.cancel_nonterminal_attempts(
            conn,
            run_id=run_id,
            completion_fence_id=fence_id,
            now=now,
        )
    recovered = _abort_service(store, backend).abort_run(run_id)
    assert recovered.termination_pending is False
    assert recovered.process_action is AbortProcessAction.TERMINATED


def test_abort_between_completed_attempts_releases_resources(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    store, run_id, tick = _launch_active_attempt(tmp_path, backend=backend)
    tick.run_once()
    result = _abort_service(store, backend).abort_run(run_id)
    assert result.termination_pending is False
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
    _assert_abort_resources_released(store, run_id)
    follow_up = tick.run_once()
    assert not any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in follow_up.run_receipts
    )


def test_abort_during_usage_limit_waiting_releases_resources(tmp_path: Path) -> None:
    from ai_dev_loop.scheduler.domain.state import (
        AdmittedRunCheckpoint,
        CursorWorkflowCheckpoint,
        WaitingUsageLimitState,
    )

    store, _, run_id = _bootstrap_run(tmp_path, repo_root=str(tmp_path / "repo"))
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    checkpoint = AdmittedRunCheckpoint(
        authorized_at="2026-09-09T11:00:00.000000Z",
        authorized_controller_session_id=CONTROLLER_SESSION,
        admitted_at="2026-09-09T11:05:00.000000Z",
        admission_status_artifact_path="admission/status.txt",
        admission_status_sha256="c" * 64,
    )
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        wait_state = WaitingUsageLimitState(
            run_id=run_id,
            version=version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            updated_at="2026-09-09T12:00:00.000000Z",
            context=state.context,
            checkpoint=checkpoint,
            cursor=CursorWorkflowCheckpoint(
                chat_id="22222222-2222-2222-2222-222222222222",
                wait_until="2026-09-09T13:00:00.000000Z",
                usage_limit_fingerprint_path="git/cursor-output/01.usage-limit-failure.json",
                usage_limit_fingerprint_sha256="a" * 64,
                original_prompt_path="prompts/cursor-initial.txt",
                original_prompt_sha256="b" * 64,
            ),
        )
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=wait_state,
            now=now,
        )
        conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = ?, holder_claim_id = 'claim-usage-limit',
                holder_tick_generation = 1, updated_at = ?
            WHERE capacity_name = 'global_active_agent'
            """,
            (run_id, now.isoformat()),
        )
    result = _abort_service(store, FakeAgentProcessBackend()).abort_run(run_id)
    assert result.termination_pending is False
    _assert_abort_resources_released(store, run_id)


def test_persist_attempt_completion_race_releases_resources(tmp_path: Path) -> None:
    attempt_id = "att-" + "f" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=5,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    store, run_id, tick = _launch_active_attempt(tmp_path, backend=backend, attempt_id=attempt_id)
    _abort_service(store, backend).abort_run(run_id)
    with store.begin_read() as conn:
        attempt = store.get_attempt_by_id(conn, attempt_id)
        assert attempt is not None
        assert str(attempt["status"]) == ATTEMPT_STATUS_CANCELLED
        assert int(attempt["ingested"]) == 0
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id
        dispatch_id = str(attempt["dispatch_id"])
        claim_id = str(attempt["capacity_claim_id"])
        unit_identity = str(attempt["unit_identity"])
        capacity_tick_generation = int(attempt["capacity_tick_generation"])
    now = datetime(2026, 9, 9, 12, 5, tzinfo=UTC)
    tick_owner = "tick-persist-race"
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id=tick_owner,
            now=now,
            ttl_seconds=60,
        )
        assert lease is not None
        generation, _ = lease
    receipt = tick._attempt_service._persist_attempt_completion(
        tick_owner_id=tick_owner,
        tick_lease_generation=generation,
        run_id=run_id,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        claim_id=claim_id,
        unit_identity=unit_identity,
        capacity_tick_generation=capacity_tick_generation,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        envelope_sha256="e" * 64,
    )
    with store.begin_immediate() as conn:
        store.release_global_tick_lease(
            conn,
            owner_id=tick_owner,
            generation=generation,
            now=now,
        )
    assert receipt.action == "attempt_result_stale"
    assert receipt.detail == "run_aborted"
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT ingested FROM scheduler_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert row is not None
        assert int(row[0]) == 1
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
    _assert_abort_resources_released(store, run_id)


def test_reconcile_existing_abort_runs_outside_active_write_transaction(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    backend = FakeAgentProcessBackend()
    service = _abort_service(store, backend)
    active_writes = 0
    original_begin_immediate = store.begin_immediate
    reconcile_depths: list[int] = []

    @contextmanager
    def tracking_begin_immediate():
        nonlocal active_writes
        active_writes += 1
        with original_begin_immediate() as conn:
            yield conn
        active_writes -= 1

    store.begin_immediate = tracking_begin_immediate  # type: ignore[method-assign]

    original_reconcile = SchedulerAbortService._reconcile_existing_abort

    def tracking_reconcile(self, run_id_arg: str, **kwargs: object) -> object:
        reconcile_depths.append(active_writes)
        return original_reconcile(self, run_id_arg, **kwargs)

    SchedulerAbortService._reconcile_existing_abort = tracking_reconcile  # type: ignore[method-assign]

    first = service.abort_run(run_id)
    second = service.abort_run(run_id)

    SchedulerAbortService._reconcile_existing_abort = original_reconcile  # type: ignore[method-assign]

    assert first.abort_persisted is True
    assert second.idempotent_replay is True
    assert reconcile_depths
    assert all(depth == 0 for depth in reconcile_depths)
    _assert_abort_resources_released(store, run_id)


def test_late_completion_after_abort_records_stale_without_busy_error(tmp_path: Path) -> None:
    attempt_id = "att-" + "e" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=5,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    store, run_id, tick = _launch_active_attempt(tmp_path, backend=backend, attempt_id=attempt_id)
    _abort_service(store, backend).abort_run(run_id)
    backend.set_scenario(attempt_id, FakeAttemptScenario(active_ticks=0, exit_code=0))
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT status, ingested FROM scheduler_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert row is not None
        assert str(row[0]) == ATTEMPT_STATUS_CANCELLED
        assert int(row[1]) == 0
    receipt = tick.run_once()
    assert any(item.action == "attempt_result_stale" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
    _assert_abort_resources_released(store, run_id)


def test_status_waiting_and_blocked_safe_actions_include_details(tmp_path: Path) -> None:
    from tests.unit.scheduler.helpers import sample_submitted_state

    from ai_dev_loop.scheduler.application.contracts import summary_from_context
    from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
    from ai_dev_loop.scheduler.domain.state import (
        AdmittedRunCheckpoint,
        BlockedState,
        CursorWorkflowCheckpoint,
        WaitingUsageLimitState,
    )

    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    submitted = sample_submitted_state(repo_root="/tmp/repo")
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    checkpoint = AdmittedRunCheckpoint(
        authorized_at="2026-09-09T11:00:00.000000Z",
        authorized_controller_session_id=CONTROLLER_SESSION,
        admitted_at="2026-09-09T11:05:00.000000Z",
        admission_status_artifact_path="admission/status.txt",
        admission_status_sha256="c" * 64,
    )
    wait_state = WaitingUsageLimitState(
        run_id=submitted.run_id,
        version=1,
        idempotency_key=submitted.idempotency_key,
        submitted_at=submitted.submitted_at,
        updated_at="2026-09-09T12:00:00.000000Z",
        context=submitted.context,
        checkpoint=checkpoint,
        cursor=CursorWorkflowCheckpoint(
            chat_id="22222222-2222-2222-2222-222222222222",
            wait_until="2026-09-09T13:00:00.000000Z",
            usage_limit_fingerprint_path="git/cursor-output/01.usage-limit-failure.json",
            usage_limit_fingerprint_sha256="a" * 64,
            original_prompt_path="prompts/cursor-initial.txt",
            original_prompt_sha256="b" * 64,
        ),
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=submitted.run_id,
            state=submitted,
            event_id="evt-submit",
            event=RunSubmittedEvent(
                run_id=submitted.run_id,
                idempotency_key=submitted.idempotency_key,
                worktree_key=submitted.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=now,
        )
        store.compare_and_swap_state(
            conn,
            run_id=submitted.run_id,
            expected_version=1,
            new_state=wait_state,
            now=now,
        )
    status = SchedulerStatusService(store).get_status(submitted.run_id)
    assert status.summary.safe_next_action.kind == SafeNextActionKind.WAIT_UNTIL
    assert "2026-09-09T13:00:00.000000Z" in (status.summary.safe_next_action.command or "")

    from ai_dev_loop.scheduler.domain.common import worktree_key

    blocked_run_id = "run-blocked-status"
    blocked_idempotency = "d" * 64
    blocked_repo = "/tmp/repo-blocked"
    blocked_context = submitted.context.model_copy(
        update={
            "repository": submitted.context.repository.model_copy(
                update={
                    "root": blocked_repo,
                    "worktree_key": worktree_key(blocked_repo),
                }
            )
        }
    )
    blocked_submitted = submitted.model_copy(
        update={
            "run_id": blocked_run_id,
            "idempotency_key": blocked_idempotency,
            "context": blocked_context,
        }
    )
    blocked_state = BlockedState(
        run_id=blocked_run_id,
        version=1,
        idempotency_key=blocked_idempotency,
        submitted_at=submitted.submitted_at,
        updated_at="2026-09-09T12:05:00.000000Z",
        context=blocked_context,
        blocked_at="2026-09-09T12:05:00.000000Z",
        block_reason_kind="usage_limit_fingerprint_drift",
        block_reason_summary="usage-limit fingerprint drift blocked retry",
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=blocked_run_id,
            state=blocked_submitted,
            event_id="evt-blocked",
            event=RunSubmittedEvent(
                run_id=blocked_run_id,
                idempotency_key=blocked_idempotency,
                worktree_key=submitted.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=now,
        )
        store.compare_and_swap_state(
            conn,
            run_id=blocked_run_id,
            expected_version=1,
            new_state=blocked_state,
            now=now,
        )
    blocked_status = SchedulerStatusService(store).get_status(blocked_run_id)
    assert blocked_status.summary.safe_next_action.kind == SafeNextActionKind.INSPECT_BLOCKED
    assert "usage_limit_fingerprint_drift" in (
        blocked_status.summary.safe_next_action.command or ""
    )
    listed = SchedulerStatusService(store).list_runs()
    by_id = {item.run_id: item for item in listed}
    assert by_id[submitted.run_id].safe_next_action.kind == SafeNextActionKind.WAIT_UNTIL
    assert by_id[blocked_run_id].safe_next_action.kind == SafeNextActionKind.INSPECT_BLOCKED

    candidate = load_scheduler_candidate(
        run_id=submitted.run_id,
        controller_session_id=CONTROLLER_SESSION,
        repository_root=Path("/tmp/repo"),
        db_path=db,
    )
    assert candidate.safe_next_action.kind == SafeNextActionKind.WAIT_UNTIL
    candidates = find_scheduler_candidates(
        controller_session_id=CONTROLLER_SESSION,
        repository_root=Path("/tmp/repo"),
        include_terminal=True,
        db_path=db,
    )
    assert any(
        item.run_id == submitted.run_id
        and item.safe_next_action.kind == SafeNextActionKind.WAIT_UNTIL
        for item in candidates
    )
    wait_summary = summary_from_context(
        run_id=submitted.run_id,
        state_kind="waiting_usage_limit",
        submitted_at=submitted.submitted_at,
        updated_at=submitted.updated_at,
        context=submitted.context,
        cursor_wait_until="2026-09-09T13:00:00.000000Z",
    )
    assert wait_summary.safe_next_action.kind == SafeNextActionKind.WAIT_UNTIL

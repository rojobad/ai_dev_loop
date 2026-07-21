"""Deterministic event-driven lease renewal safety matrix for EffectWorker."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

from tests.integration.phase16_4_matrix_helpers import make_engine
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.contracts import (
    EventDisposition,
    EventSubmission,
    LeaseHeartbeatResult,
    LeaseStatus,
    WorkerStepResult,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    EffectCompletionToken,
    PauseReasonKind,
    RepositoryIdentity,
    SafeAction,
    SafeActionKind,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.effects import PrReviewEffect
from ai_dev_loop.pr_review_v2.domain.events import EffectBlocked, PrReviewEvent, StartRequested
from ai_dev_loop.pr_review_v2.domain.state import PreparedState
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker, LeaseRenewalCoordinator

SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


class IntervalController:
    """Event-driven stoppable interval wait for lease-renewal tests."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._action: str | None = None

    def wait(self, timeout: float) -> bool:
        del timeout
        with self._cond:
            while self._action is None:
                self._cond.wait()
            action = self._action
            self._action = None
            return action in {"wake", "stop"}

    def wake_for_stop(self) -> None:
        with self._cond:
            self._action = "stop"
            self._cond.notify_all()

    def fire_interval(self) -> None:
        with self._cond:
            self._action = "interval"
            self._cond.notify_all()


class BlockingExecutor:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.raised: Exception | None = None

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> PrReviewEvent:
        del effect, now
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5.0)
        if self.raised is not None:
            raise self.raised
        return EffectBlocked(
            occurred_at=self.clock.now(),
            token=token,
            reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            safe_action=SafeAction(
                kind=SafeActionKind.INSPECT_ARTIFACTS,
                condition="test",
            ),
            safe_summary="blocked for test",
        )


class ImmediateExecutor:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> PrReviewEvent:
        del effect, now
        self.calls += 1
        return EffectBlocked(
            occurred_at=self.clock.now(),
            token=token,
            reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            safe_action=SafeAction(
                kind=SafeActionKind.INSPECT_ARTIFACTS,
                condition="test",
            ),
            safe_summary="short execution",
        )


def _prepared(clock: FakeClock, *, run_id: str = "run-lease-matrix") -> PreparedState:
    return PreparedState(
        run_id=run_id,
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256=HASH_1),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json",
                sha256=HASH_2,
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=clock.now(),
    )


def _start_engine(
    tmp_path: Path, clock: FakeClock, *, prefix: str
) -> tuple[PrReviewEngine, PreparedState]:
    engine = make_engine(tmp_path / f"{prefix}.sqlite3", clock, prefix=prefix)
    prepared = _prepared(clock, run_id=f"run-{prefix}")
    engine.create_run(prepared.run_id, prepared)
    engine.apply_event(
        EventSubmission(
            submission_id="start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    return engine, prepared


def _snapshot_fingerprint(engine: PrReviewEngine, run_id: str) -> tuple:
    """Run snapshot + timers/outbox/effects identity (excludes event journal rows)."""

    with engine.store.begin_read() as conn:
        run = conn.execute(
            "SELECT version, state_kind, state_payload_sha256 FROM pr_review_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        timers = conn.execute(
            """
            SELECT timer_id, status, due_at FROM pr_review_timers
            WHERE run_id=? ORDER BY timer_id
            """,
            (run_id,),
        ).fetchall()
        effects = conn.execute(
            """
            SELECT dispatch_id, status, claim_id, claim_lease_generation
            FROM pr_review_effects WHERE run_id=? ORDER BY dispatch_id
            """,
            (run_id,),
        ).fetchall()
        outbox = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='pr_review_outbox'
            """
        ).fetchone()
        outbox_rows: tuple = ()
        if outbox is not None:
            outbox_rows = tuple(
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM pr_review_outbox WHERE run_id=? ORDER BY 1",
                    (run_id,),
                ).fetchall()
            )
    return (
        int(run["version"]),
        str(run["state_kind"]),
        str(run["state_payload_sha256"]),
        tuple((str(r["timer_id"]), str(r["status"]), str(r["due_at"])) for r in timers),
        tuple(
            (
                str(r["dispatch_id"]),
                str(r["status"]),
                str(r["claim_id"]) if r["claim_id"] is not None else None,
                int(r["claim_lease_generation"])
                if r["claim_lease_generation"] is not None
                else None,
            )
            for r in effects
        ),
        outbox_rows,
    )


def _completion_offers(engine: PrReviewEngine, run_id: str) -> int:
    with engine.store.begin_read() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM pr_review_events
            WHERE run_id=? AND disposition IN ('accepted', 'duplicate', 'stale', 'rejected')
              AND event_kind IN (
                'effect_succeeded', 'effect_retryable_failure', 'effect_blocked'
              )
            """,
            (run_id,),
        ).fetchone()
    return int(row["n"])


def _run_worker_blocked(
    *,
    engine: PrReviewEngine,
    prepared: PreparedState,
    executor: BlockingExecutor,
    controller: IntervalController,
) -> tuple[threading.Thread, dict[str, WorkerStepResult]]:
    worker = EffectWorker(
        engine,
        executor,
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
        heartbeat_interval_wait=controller,
    )
    result_box: dict[str, WorkerStepResult] = {}
    thread = threading.Thread(
        target=lambda: result_box.update(step=worker.run_once(prepared.run_id))
    )
    thread.start()
    assert executor.entered.wait(timeout=5.0)
    return thread, result_box


def test_heartbeat_rejection_marks_lease_lost_and_offers_once(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="hb-reject")
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    thread, result_box = _run_worker_blocked(
        engine=engine, prepared=prepared, executor=executor, controller=controller
    )
    heartbeat_done = threading.Event()
    real = engine.heartbeat_lease

    def counting(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        result = real(run_id, owner_id, generation)
        heartbeat_done.set()
        return result

    engine.heartbeat_lease = counting  # type: ignore[method-assign]
    clock.advance(60)
    engine.acquire_lease(prepared.run_id, "owner-b")
    before = _snapshot_fingerprint(engine, prepared.run_id)
    controller.fire_interval()
    assert heartbeat_done.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    step = result_box["step"]
    assert isinstance(step, WorkerStepResult)
    assert executor.calls == 1
    assert step.disposition is EventDisposition.STALE
    assert _snapshot_fingerprint(engine, prepared.run_id) == before
    assert _completion_offers(engine, prepared.run_id) == 1


def test_heartbeat_exception_fences_result_as_stale(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="hb-exc")
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    real = engine.heartbeat_lease
    saw_failure = threading.Event()
    calls = {"n": 0}

    def flaky(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        calls["n"] += 1
        if calls["n"] == 1:
            saw_failure.set()
            raise RuntimeError("injected heartbeat failure")
        return real(run_id, owner_id, generation)

    engine.heartbeat_lease = flaky  # type: ignore[method-assign]
    thread, result_box = _run_worker_blocked(
        engine=engine, prepared=prepared, executor=executor, controller=controller
    )
    before = _snapshot_fingerprint(engine, prepared.run_id)
    controller.fire_interval()
    assert saw_failure.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert executor.calls == 1
    step = result_box["step"]
    assert step.claimed is True
    assert step.disposition in {EventDisposition.STALE, EventDisposition.REJECTED}
    assert _snapshot_fingerprint(engine, prepared.run_id) == before
    assert _completion_offers(engine, prepared.run_id) == 1


def test_heartbeat_and_release_both_fail_still_fences_as_stale(tmp_path: Path) -> None:
    """Lost authority must fence even when durable release_lease also raises."""

    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="hb-rel-fail")
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    saw_heartbeat_failure = threading.Event()
    release_attempts = {"n": 0}
    real_heartbeat = engine.heartbeat_lease
    real_release = engine.release_lease

    def flaky_heartbeat(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        del run_id, owner_id, generation
        saw_heartbeat_failure.set()
        raise RuntimeError("injected heartbeat failure")

    def flaky_release(run_id: str, owner_id: str, generation: int):
        release_attempts["n"] += 1
        raise RuntimeError("injected release failure")

    engine.heartbeat_lease = flaky_heartbeat  # type: ignore[method-assign]
    engine.release_lease = flaky_release  # type: ignore[method-assign]
    thread, result_box = _run_worker_blocked(
        engine=engine, prepared=prepared, executor=executor, controller=controller
    )
    before = _snapshot_fingerprint(engine, prepared.run_id)
    # Prove the durable lease is still active before completion (release failed).
    with engine.store.begin_read() as conn:
        lease_row = engine.store.get_lease_row(conn, prepared.run_id)
        assert lease_row["status"] == "active"
        assert lease_row["owner_id"] == "owner-a"
    controller.fire_interval()
    assert saw_heartbeat_failure.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert executor.calls == 1
    assert release_attempts["n"] >= 1
    step = result_box["step"]
    assert step.disposition in {EventDisposition.STALE, EventDisposition.REJECTED}
    assert step.safe_detail is not None
    assert "lease authority lost" in (step.safe_detail or "")
    assert _snapshot_fingerprint(engine, prepared.run_id) == before
    assert _completion_offers(engine, prepared.run_id) == 1
    # Restore for any later assertions / cleanup clarity.
    engine.heartbeat_lease = real_heartbeat  # type: ignore[method-assign]
    engine.release_lease = real_release  # type: ignore[method-assign]


def test_abort_while_blocked_read_fences_late_result_once(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="abort-block")
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    thread, result_box = _run_worker_blocked(
        engine=engine, prepared=prepared, executor=executor, controller=controller
    )
    heartbeat_done = threading.Event()
    real = engine.heartbeat_lease

    def counting(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        result = real(run_id, owner_id, generation)
        heartbeat_done.set()
        return result

    engine.heartbeat_lease = counting  # type: ignore[method-assign]
    abort = engine.abort_run(submission_id="abort-1", run_id=prepared.run_id)
    assert abort.disposition is EventDisposition.ACCEPTED
    controller.fire_interval()
    assert heartbeat_done.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert executor.calls == 1
    assert result_box["step"].disposition is EventDisposition.STALE
    assert engine.get_status(prepared.run_id).state_kind == "aborted"
    assert _completion_offers(engine, prepared.run_id) == 1


def test_lease_replacement_then_late_executor_return_is_fenced(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="replace")
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    thread, result_box = _run_worker_blocked(
        engine=engine, prepared=prepared, executor=executor, controller=controller
    )
    clock.advance(60)
    lease_b = engine.acquire_lease(prepared.run_id, "owner-b")
    assert lease_b.generation >= 2
    before = _snapshot_fingerprint(engine, prepared.run_id)
    heartbeat_done = threading.Event()
    real = engine.heartbeat_lease

    def counting(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        result = real(run_id, owner_id, generation)
        heartbeat_done.set()
        return result

    engine.heartbeat_lease = counting  # type: ignore[method-assign]
    controller.fire_interval()
    assert heartbeat_done.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert executor.calls == 1
    assert result_box["step"].disposition is EventDisposition.STALE
    assert _snapshot_fingerprint(engine, prepared.run_id) == before
    assert _completion_offers(engine, prepared.run_id) == 1


def test_executor_exception_stops_renewal_without_second_call(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="exec-exc")
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    executor.raised = RuntimeError("executor boom")
    worker = EffectWorker(
        engine,
        executor,
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
        heartbeat_interval_wait=controller,
    )
    thread_exc: list[BaseException] = []

    def run() -> None:
        try:
            worker.run_once(prepared.run_id)
        except BaseException as exc:  # noqa: BLE001
            thread_exc.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert executor.entered.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert executor.calls == 1
    assert thread_exc and "executor boom" in str(thread_exc[0])


def test_normal_short_execution_skips_heartbeat_and_completes(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, prepared = _start_engine(tmp_path, clock, prefix="short")
    controller = IntervalController()
    executor = ImmediateExecutor(clock)
    worker = EffectWorker(
        engine,
        executor,
        owner_id="owner-a",
        heartbeat_interval=timedelta(seconds=1),
        heartbeat_interval_wait=controller,
    )
    step = worker.run_once(prepared.run_id)
    assert step.claimed is True
    assert step.disposition is EventDisposition.ACCEPTED
    assert executor.calls == 1


def test_stop_terminates_thread_without_controller_cleanup(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "stop.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="stop"),
        lease_ttl=timedelta(seconds=30),
    )
    prepared = _prepared(clock, run_id="run-stop")
    engine.create_run(prepared.run_id, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    controller = IntervalController()
    heartbeats = {"n": 0}
    real = engine.heartbeat_lease

    def counting(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        heartbeats["n"] += 1
        return real(run_id, owner_id, generation)

    engine.heartbeat_lease = counting  # type: ignore[method-assign]
    coord = LeaseRenewalCoordinator(
        engine,
        run_id=prepared.run_id,
        owner_id="owner-a",
        generation=lease.generation,
        interval=timedelta(seconds=1),
        interval_wait=controller,
    )
    coord.start()
    assert coord.thread_alive is True
    # stop() alone must wake the injected wait and join the thread.
    coord.stop()
    assert coord.thread_alive is False
    after = heartbeats["n"]
    controller.fire_interval()
    assert heartbeats["n"] == after


def test_bounded_stop_join_and_no_second_external_call(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "coord.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="coord"),
        lease_ttl=timedelta(seconds=30),
    )
    prepared = _prepared(clock, run_id="run-coord")
    engine.create_run(prepared.run_id, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    controller = IntervalController()
    heartbeat_done = threading.Event()
    real = engine.heartbeat_lease
    heartbeats = {"n": 0}

    def counting(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        heartbeats["n"] += 1
        result = real(run_id, owner_id, generation)
        heartbeat_done.set()
        return result

    engine.heartbeat_lease = counting  # type: ignore[method-assign]
    coord = LeaseRenewalCoordinator(
        engine,
        run_id=prepared.run_id,
        owner_id="owner-a",
        generation=lease.generation,
        interval=timedelta(seconds=1),
        interval_wait=controller,
    )
    coord.start()
    controller.fire_interval()
    assert heartbeat_done.wait(timeout=5.0)
    coord.stop()
    assert coord.thread_alive is False
    assert coord.heartbeat_count >= 1
    assert coord.lease_lost is False
    after = heartbeats["n"]
    controller.fire_interval()
    assert heartbeats["n"] == after


def test_coordinator_heartbeat_success_path(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "ok.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="ok"),
        lease_ttl=timedelta(seconds=30),
    )
    prepared = _prepared(clock, run_id="run-ok")
    engine.create_run(prepared.run_id, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    controller = IntervalController()
    heartbeat_done = threading.Event()
    real = engine.heartbeat_lease

    def counting(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        result = real(run_id, owner_id, generation)
        heartbeat_done.set()
        return result

    engine.heartbeat_lease = counting  # type: ignore[method-assign]
    coord = LeaseRenewalCoordinator(
        engine,
        run_id=prepared.run_id,
        owner_id="owner-a",
        generation=lease.generation,
        interval=timedelta(seconds=1),
        interval_wait=controller,
    )
    coord.start()
    controller.fire_interval()
    assert heartbeat_done.wait(timeout=5.0)
    coord.stop()
    assert coord.thread_alive is False
    assert coord.heartbeat_count >= 1
    assert coord.lease_lost is False
    hb = engine.heartbeat_lease(prepared.run_id, "owner-a", lease.generation)
    assert hb.accepted is True
    assert hb.status is LeaseStatus.ACTIVE

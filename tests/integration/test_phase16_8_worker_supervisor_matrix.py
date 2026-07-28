"""Phase 16.8 Step 8: timer, lease, worker, and supervisor production-boundary matrix."""

from __future__ import annotations

import contextlib
import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    drive_to_waiting_for_bot,
    fingerprint,
    make_engine,
    make_observe_eligible,
)
from tests.integration.phase16_8_checkpoint_helpers import reopen_engine
from tests.integration.test_phase16_8_control_matrix import PLAN_BYTES, PROMPT_BYTES, _ctx
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run

from ai_dev_loop.launcher import read_process_pgid, read_process_starttime
from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
)
from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectClassification,
    EffectRetryableFailure,
    ErrorSummary,
    PreparedState,
    RepositoryIdentity,
    ResumeRequested,
    SourceRunOrigin,
    StartRequested,
    TransientErrorKind,
    WorkflowLimits,
    classify_effect,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import FaultInjector
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
)

SHA_A = "a" * 40


def _ctx_for(repo_root: str, *, source_run_id: str = "src-step8"):
    ctx = _ctx(repo_root=repo_root)
    return ctx.model_copy(
        update={"run_binding": ctx.run_binding.model_copy(update={"source_run_id": source_run_id})}
    )


def _start_run(engine: PrReviewEngine, prepared: PreparedState, *, submission_id: str) -> None:
    engine.create_run(prepared.run_id, prepared)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id=submission_id,
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


def _prepared(clock: FakeClock, *, run_id: str = "run-p168-step8") -> PreparedState:
    return PreparedState(
        run_id=run_id,
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256="1" * 64),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json",
                sha256="2" * 64,
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=clock.now(),
    )


def test_timer_duplicate_fire_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock, prefix="p168-timer-dup")
    prepared = _prepared(clock)
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    next_at = clock.now() + timedelta(seconds=5)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="retry-1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="down"),
                failed_attempt=1,
                next_attempt_at=next_at,
            ),
        )
    )
    clock.set(next_at)
    first = engine.fire_due_timers_for_run(prepared.run_id)
    assert len(first) == 1
    assert first[0].disposition is EventDisposition.ACCEPTED
    assert engine.get_status(prepared.run_id).effect_attempt == 2
    second = engine.fire_due_timers_for_run(prepared.run_id)
    assert second == []


def test_timer_per_run_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock, prefix="p168-timer-iso")
    prepared_a = _prepared(clock, run_id="run-a")
    prepared_b = _prepared(clock, run_id="run-b")
    _start_run(engine, prepared_a, submission_id="sub-start-a")
    _start_run(engine, prepared_b, submission_id="sub-start-b")
    for prepared in (prepared_a, prepared_b):
        lease = engine.acquire_lease(prepared.run_id, "worker-a")
        claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
        assert claim is not None
        engine.complete_claim(
            EffectCompletionRequest(
                submission_id=f"retry-{prepared.run_id}-{claim.effect.effect_id}",
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id="worker-a",
                lease_generation=lease.generation,
                event=EffectRetryableFailure(
                    occurred_at=clock.now(),
                    token=claim.completion_token,
                    error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="t"),
                    failed_attempt=1,
                    next_attempt_at=clock.now() + timedelta(seconds=10),
                ),
            )
        )
    clock.advance(timedelta(seconds=10))
    fired_a = engine.fire_due_timers_for_run(prepared_a.run_id)
    fired_b = engine.fire_due_timers_for_run(prepared_b.run_id)
    assert len(fired_a) == 1
    assert len(fired_b) == 1
    assert fired_a[0].run_id == prepared_a.run_id
    assert fired_b[0].run_id == prepared_b.run_id


def test_timer_superseded_after_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock, prefix="p168-timer-super")
    prepared = _prepared(clock)
    start_run(engine, prepared)
    lease, claim = claim_next(engine, prepared.run_id)
    next_at = clock.now() + timedelta(seconds=30)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="retry-local",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="down"),
                failed_attempt=1,
                next_attempt_at=next_at,
            ),
        )
    )
    with engine._store.begin_read() as conn:  # noqa: SLF001
        pending = conn.execute(
            """
            SELECT timer_id, status FROM pr_review_timers
            WHERE run_id = ? AND status = 'pending'
            """,
            (prepared.run_id,),
        ).fetchone()
    assert pending is not None
    timer_id = str(pending["timer_id"])

    engine.abort_run(submission_id="abort-timer", run_id=prepared.run_id)
    assert engine.get_status(prepared.run_id).state_kind == "aborted"
    with engine._store.begin_read() as conn:  # noqa: SLF001
        superseded = conn.execute(
            "SELECT status FROM pr_review_timers WHERE timer_id = ?",
            (timer_id,),
        ).fetchone()
    assert superseded is not None
    assert superseded["status"] == "superseded"

    clock.set(next_at)
    assert engine.fire_due_timers_for_run(prepared.run_id) == []
    stale = engine._fire_one_timer(timer_id, now=next_at)  # noqa: SLF001
    assert stale.disposition is EventDisposition.DUPLICATE
    assert stale.superseded is True


def test_six_attempt_exhaustion_then_resume_new_batch_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock, prefix="p168-six")
    prepared = _prepared(clock)
    start_run(engine, prepared)
    original_id: str | None = None
    original_key: str | None = None
    for attempt in range(1, 7):
        if attempt > 1:
            clock.advance(timedelta(seconds=31))
            status = engine.get_status(prepared.run_id)
            if status.next_eligible_at is not None:
                clock.set(status.next_eligible_at)
            engine.fire_due_timers_for_run(prepared.run_id)
        lease, claim = claim_next(engine, prepared.run_id)
        if original_id is None:
            original_id = claim.effect.effect_id
            original_key = claim.effect.idempotency_key
        next_at = clock.now() + timedelta(seconds=30)
        engine.complete_claim(
            EffectCompletionRequest(
                submission_id=f"fail-{attempt}",
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id="owner-a",
                lease_generation=lease.generation,
                event=EffectRetryableFailure(
                    occurred_at=clock.now(),
                    token=claim.completion_token,
                    error=ErrorSummary(kind=TransientErrorKind.HTTP_429, safe_summary="rate"),
                    failed_attempt=attempt,
                    next_attempt_at=next_at,
                ),
            )
        )
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "paused"
    engine.apply_event(
        EventSubmission(
            submission_id="resume-new-batch",
            run_id=prepared.run_id,
            expected_version=status.run_version,
            event=ResumeRequested(occurred_at=clock.now()),
        )
    )
    lease, claim = claim_next(engine, prepared.run_id)
    assert claim.effect.effect_id == original_id
    assert claim.effect.idempotency_key == original_key
    assert claim.attempt == 1


def test_two_workers_readonly_same_dispatch_after_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock, prefix="p168-two-read")
    prepared = _prepared(clock)
    start_run(engine, prepared)
    drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, prepared.run_id, clock)
    lease_a, claim_a = claim_next(engine, prepared.run_id, owner="owner-a")
    dispatch_id = claim_a.dispatch_id
    effect_id = claim_a.effect.effect_id
    clock.advance(timedelta(seconds=31))
    engine2 = reopen_engine(tmp_path / "db.sqlite3", clock, prefix="p168-two-read-re")
    lease_b = engine2.acquire_lease(prepared.run_id, "owner-b")
    engine2.recover_expired_claims(prepared.run_id, "owner-b", lease_b.generation)
    result = engine2.claim_next_effect(prepared.run_id, "owner-b", lease_b.generation)
    assert result.claim is not None
    assert result.claim.dispatch_id == dispatch_id
    assert result.claim.effect.effect_id == effect_id


def test_stale_completion_after_lease_replacement_fences_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_phase16_6_crash_restart_and_fencing import (
        test_stale_completion_after_replacement_is_fenced,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    test_stale_completion_after_replacement_is_fenced(tmp_path)


def test_control_start_spawn_failed_then_repair(tmp_path: Path) -> None:
    clock = FakeClock()
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-spawn-fail")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    calls = {"n": 0}

    def spawner(_run_id: str) -> str:
        calls["n"] += 1
        return "spawn_failed" if calls["n"] == 1 else "spawned"

    prep = PreparationService(engine, arts, clock=clock)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-spawn",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx_for(repo_root=str(repo.resolve()), source_run_id="src-spawn"),
        )
    )
    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=spawner,
    )
    first = control.start(created.run_id)
    assert first.supervisor_action == "spawn_failed"
    second = control.start(created.run_id)
    assert second.supervisor_action in {"spawned", "repaired"}


def test_control_start_reuses_live_supervisor_metadata(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock, prefix="p168-reuse")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    prep = PreparationService(engine, arts, clock=clock)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-reuse",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"patch\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx_for(repo_root=str(repo.resolve()), source_run_id="src-reuse"),
        )
    )
    spawn_calls = {"n": 0}

    def spawner(_run_id: str) -> str:
        spawn_calls["n"] += 1
        return "spawned"

    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=spawner,
    )
    control.start(created.run_id)
    pid = os.getpid()
    pgid = read_process_pgid(pid) or os.getpgid(0)
    starttime = read_process_starttime(pid)
    assert starttime is not None
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=created.run_id,
            token="live-token",
            pid=pid,
            pgid=pgid,
            process_start_time=str(starttime),
            executable=str(Path(f"/proc/{pid}/exe").resolve()),
            created_at=clock.now().isoformat(),
        )
    )
    again = control.start(created.run_id)
    assert again.supervisor_action in {"reused", "repaired", "spawned"}
    assert spawn_calls["n"] == 1


def test_control_start_repairs_stale_supervisor_metadata(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock, prefix="p168-stale")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    prep = PreparationService(engine, arts, clock=clock)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-stale",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"patch\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx_for(repo_root=str(repo.resolve()), source_run_id="src-stale"),
        )
    )
    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=lambda _rid: "spawned",
    )
    control.start(created.run_id)
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=created.run_id,
            token="stale-token",
            pid=999999,
            pgid=999999,
            process_start_time="0",
            executable="/bin/false",
            created_at=clock.now().isoformat(),
        )
    )
    repaired = control.start(created.run_id)
    assert repaired.supervisor_action in {"repaired", "spawned"}


def test_sqlite_reopen_preserves_timer_and_claim_invariants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-reopen")
    prepared = _prepared(clock)
    start_run(engine, prepared)
    lease, claim = claim_next(engine, prepared.run_id)
    next_at = clock.now() + timedelta(seconds=30)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="retry-reopen",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="down"),
                failed_attempt=1,
                next_attempt_at=next_at,
            ),
        )
    )
    before = engine.get_status(prepared.run_id)
    engine2 = reopen_engine(db_path, clock, prefix="p168-reopen-2")
    after = engine2.get_status(prepared.run_id)
    assert after.state_kind == before.state_kind == "waiting_retry"
    assert after.next_eligible_at == before.next_eligible_at
    assert engine2.fire_due_timers_for_run(prepared.run_id) == []


def _arm_waiting_retry(engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock):
    lease, claim = claim_next(engine, prepared.run_id)
    next_at = clock.now() + timedelta(seconds=5)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id=f"retry-arm-{claim.effect.effect_id}",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="down"),
                failed_attempt=1,
                next_attempt_at=next_at,
            ),
        )
    )
    return next_at


def test_timer_fire_crash_before_commit_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    faults = FaultInjector()
    db_path = tmp_path / "db.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-timer-pre", fault_hook=faults)
    prepared = _prepared(clock, run_id="run-timer-pre")
    start_run(engine, prepared)
    next_at = _arm_waiting_retry(engine, prepared, clock)
    before = fingerprint(engine, prepared.run_id)
    clock.set(next_at)

    def boom() -> None:
        raise RuntimeError("fault:pre_commit")

    faults.set("pre_commit", boom)
    with pytest.raises(RuntimeError, match="pre_commit"):
        engine.fire_due_timers_for_run(prepared.run_id)
    faults.clear()
    after = fingerprint(engine, prepared.run_id)
    assert after == before
    engine2 = reopen_engine(db_path, clock, prefix="p168-timer-pre-re")
    assert engine2.get_status(prepared.run_id).state_kind == "waiting_retry"
    fired = engine2.fire_due_timers_for_run(prepared.run_id)
    assert len(fired) == 1
    assert fired[0].disposition is EventDisposition.ACCEPTED


def test_timer_fire_crash_after_commit_dedupes_on_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "db.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-timer-post")
    prepared = _prepared(clock, run_id="run-timer-post")
    start_run(engine, prepared)
    next_at = _arm_waiting_retry(engine, prepared, clock)
    clock.set(next_at)
    first = engine.fire_due_timers_for_run(prepared.run_id)
    assert len(first) == 1
    assert first[0].disposition is EventDisposition.ACCEPTED
    # Crash after the timer transaction committed: reopen and prove idempotent fire.
    engine2 = reopen_engine(db_path, clock, prefix="p168-timer-post-re")
    assert engine2.get_status(prepared.run_id).effect_attempt == 2
    assert engine2.fire_due_timers_for_run(prepared.run_id) == []


def test_concurrent_duplicate_timer_firers_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "db.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-timer-race", busy_timeout_ms=15000)
    prepared = _prepared(clock, run_id="run-timer-race")
    start_run(engine, prepared)
    next_at = _arm_waiting_retry(engine, prepared, clock)
    clock.set(next_at)
    barrier = threading.Barrier(2)
    results: list[list] = []
    errors: list[BaseException] = []

    def firer() -> None:
        try:
            eng = make_engine(db_path, clock, prefix="p168-timer-race-w", busy_timeout_ms=15000)
            barrier.wait(timeout=5)
            results.append(eng.fire_due_timers_for_run(prepared.run_id))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=firer), threading.Thread(target=firer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors, errors
    accepted = [r for batch in results for r in batch if r.disposition is EventDisposition.ACCEPTED]
    duplicates = [
        r
        for batch in results
        for r in batch
        if r.disposition in {EventDisposition.DUPLICATE, EventDisposition.STALE}
    ]
    assert len(accepted) == 1
    assert len(accepted) + len(duplicates) >= 1
    assert engine.get_status(prepared.run_id).effect_attempt == 2


@pytest.mark.parametrize(
    "authority",
    [
        EffectClassification.LOCAL,
        EffectClassification.READ_ONLY,
        EffectClassification.MUTATING,
        EffectClassification.RECONCILING,
    ],
)
def test_two_workers_reclaim_same_dispatch_per_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: EffectClassification,
) -> None:
    """Two real EffectWorker instances share one persistent SQLite database."""

    from tests.unit.pr_review_v2.test_effect_worker_heartbeat import (
        BlockingExecutor,
        IntervalController,
    )

    from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / f"auth-{authority.value}.sqlite3"
    engine = make_engine(db_path, clock, prefix=f"p168-{authority.value}")
    prepared = _prepared(clock, run_id=f"run-{authority.value}")
    start_run(engine, prepared)

    if authority is EffectClassification.READ_ONLY:
        drive_to_waiting_for_bot(engine, prepared, clock)
        make_observe_eligible(engine, prepared.run_id, clock)
        # Drive helpers leave an active lease; expire it before worker A acquires.
        clock.advance(timedelta(seconds=31))
    elif authority is EffectClassification.MUTATING:
        lease, claim = claim_next(engine, prepared.run_id, owner="setup")
        engine.complete_claim(
            EffectCompletionRequest(
                submission_id="pub-for-mut",
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id="setup",
                lease_generation=lease.generation,
                event=publication_success(claim.effect, claim.completion_token, clock.now()),
            )
        )
        engine.release_lease(prepared.run_id, "setup", lease.generation)
    elif authority is EffectClassification.RECONCILING:
        lease, claim = claim_next(engine, prepared.run_id, owner="setup")
        engine.complete_claim(
            EffectCompletionRequest(
                submission_id="pub-for-rec",
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id="setup",
                lease_generation=lease.generation,
                event=publication_success(claim.effect, claim.completion_token, clock.now()),
            )
        )
        _lease_m, _claim_m = claim_next(engine, prepared.run_id, owner="setup")
        clock.advance(timedelta(seconds=31))
        lease_recover = engine.acquire_lease(prepared.run_id, "setup-b")
        assert (
            engine.recover_expired_claims(prepared.run_id, "setup-b", lease_recover.generation) == 1
        )
        assert engine.get_status(prepared.run_id).state_kind == "reconciling_write"
        engine.release_lease(prepared.run_id, "setup-b", lease_recover.generation)

    class AuthorityBlocking(BlockingExecutor):
        def execute(self, effect, token, *, now, **_kwargs):  # noqa: ANN001
            del effect
            self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=60.0)
            if self.raised is not None:
                raise self.raised
            from ai_dev_loop.pr_review_v2.domain.common import (
                PauseReasonKind,
                SafeAction,
                SafeActionKind,
            )
            from ai_dev_loop.pr_review_v2.domain.events import EffectBlocked

            return EffectBlocked(
                occurred_at=now if now is not None else self.clock.now(),
                token=token,
                reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
                safe_action=SafeAction(
                    kind=SafeActionKind.INSPECT_ARTIFACTS,
                    condition="test",
                ),
                safe_summary="blocked for test",
            )

    controller_a = IntervalController()
    executor_a = AuthorityBlocking(clock)
    worker_a = EffectWorker(
        engine,
        executor_a,
        owner_id="worker-a",
        heartbeat_interval=timedelta(seconds=1),
        heartbeat_interval_wait=controller_a,
    )
    box_a: dict = {}
    thread_a = threading.Thread(
        target=lambda: box_a.update(step=worker_a.run_once(prepared.run_id)),
        name=f"worker-a-{authority.value}",
    )
    thread_a.start()
    assert executor_a.entered.wait(timeout=5.0)
    assert executor_a.calls == 1

    with engine.store.begin_read() as conn:
        claimed_row = conn.execute(
            """
            SELECT * FROM pr_review_effects
            WHERE run_id=? AND status='claimed'
            ORDER BY dispatch_id DESC LIMIT 1
            """,
            (prepared.run_id,),
        ).fetchone()
    assert claimed_row is not None
    dispatch_id = str(claimed_row["dispatch_id"])
    claimed_effect = engine.store.load_validated_effect(claimed_row)
    effect_id = claimed_effect.effect_id
    assert classify_effect(claimed_effect) is authority
    lease_gen_a = engine.get_status(prepared.run_id).lease_generation

    clock.advance(timedelta(seconds=31))
    engine_b = reopen_engine(db_path, clock, prefix=f"p168-{authority.value}-b")
    controller_b = IntervalController()
    executor_b = AuthorityBlocking(clock)
    worker_b = EffectWorker(
        engine_b,
        executor_b,
        owner_id="worker-b",
        heartbeat_interval=timedelta(seconds=1),
        heartbeat_interval_wait=controller_b,
    )
    box_b: dict = {}
    thread_b = threading.Thread(
        target=lambda: box_b.update(step=worker_b.run_once(prepared.run_id)),
        name=f"worker-b-{authority.value}",
    )
    thread_b.start()

    if authority is EffectClassification.LOCAL:
        # Expired LOCAL claims pause the run; B must not re-execute the original claim.
        assert not executor_b.entered.wait(timeout=1.0)
        thread_b.join(timeout=10.0)
        assert not thread_b.is_alive()
        step_b = box_b["step"]
        assert step_b.claimed is False
        status = engine_b.get_status(prepared.run_id)
        assert status.lease_generation > lease_gen_a
        assert status.state_kind == "paused"
        before_stale = fingerprint(engine_b, prepared.run_id)
        executor_a.release.set()
        thread_a.join(timeout=10.0)
        assert not thread_a.is_alive()
        step_a = box_a["step"]
        assert step_a.claimed is True
        assert step_a.disposition is EventDisposition.STALE
        assert fingerprint(engine_b, prepared.run_id) == before_stale
        assert executor_a.calls == 1
        assert executor_b.calls == 0
        return

    # READ / MUTATING / RECONCILING: B claims under a newer lease generation.
    assert executor_b.entered.wait(timeout=5.0)
    assert executor_b.calls == 1
    with engine_b.store.begin_read() as conn:
        b_row = conn.execute(
            """
            SELECT * FROM pr_review_effects
            WHERE run_id=? AND status='claimed' AND claim_owner_id='worker-b'
            """,
            (prepared.run_id,),
        ).fetchone()
    assert b_row is not None
    b_effect = engine_b.store.load_validated_effect(b_row)
    assert engine_b.get_status(prepared.run_id).lease_generation > lease_gen_a
    if authority is EffectClassification.MUTATING:
        assert classify_effect(b_effect) is EffectClassification.RECONCILING
        assert str(b_row["dispatch_id"]) != dispatch_id
        assert engine_b.get_status(prepared.run_id).state_kind == "reconciling_write"
    else:
        assert str(b_row["dispatch_id"]) == dispatch_id
        assert b_effect.effect_id == effect_id
        assert classify_effect(b_effect) is authority
    before_stale = fingerprint(engine_b, prepared.run_id)
    executor_a.release.set()
    thread_a.join(timeout=10.0)
    assert not thread_a.is_alive()
    step_a = box_a["step"]
    assert step_a.claimed is True
    assert step_a.disposition is EventDisposition.STALE
    assert fingerprint(engine_b, prepared.run_id) == before_stale
    assert executor_a.calls == 1
    executor_b.release.set()
    thread_b.join(timeout=10.0)
    assert not thread_b.is_alive()
    step_b = box_b["step"]
    assert step_b.claimed is True
    assert step_b.disposition in {
        EventDisposition.ACCEPTED,
        EventDisposition.REJECTED,
        EventDisposition.STALE,
        EventDisposition.DUPLICATE,
    }
    assert executor_b.calls == 1


def test_heartbeat_exception_fences_with_effect_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real EffectWorker heartbeat exception fences without state mutation."""

    from tests.unit.pr_review_v2.test_effect_worker_heartbeat import (
        BlockingExecutor,
        IntervalController,
    )

    from ai_dev_loop.pr_review_v2.application.contracts import LeaseHeartbeatResult
    from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "hb-exc.sqlite3", clock, prefix="p168-hb-exc")
    prepared = _prepared(clock, run_id="run-hb-exc")
    start_run(engine, prepared)
    controller = IntervalController()
    executor = BlockingExecutor(clock)
    worker = EffectWorker(
        engine,
        executor,
        owner_id="worker-a",
        heartbeat_interval=timedelta(seconds=1),
        heartbeat_interval_wait=controller,
    )
    box: dict = {}
    thread = threading.Thread(target=lambda: box.update(step=worker.run_once(prepared.run_id)))
    thread.start()
    assert executor.entered.wait(timeout=5.0)
    before = fingerprint(engine, prepared.run_id)
    real = engine.heartbeat_lease
    saw = threading.Event()
    calls = {"n": 0}

    def flaky(run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        calls["n"] += 1
        if calls["n"] == 1:
            saw.set()
            raise RuntimeError("injected heartbeat failure")
        return real(run_id, owner_id, generation)

    engine.heartbeat_lease = flaky  # type: ignore[method-assign]
    controller.fire_interval()
    assert saw.wait(timeout=5.0)
    executor.release.set()
    thread.join(timeout=10.0)
    assert not thread.is_alive()
    step = box["step"]
    assert step.claimed is True
    assert step.disposition in {EventDisposition.STALE, EventDisposition.REJECTED}
    assert fingerprint(engine, prepared.run_id) == before
    assert executor.calls == 1


def test_launcher_metadata_persistence_failure_returns_spawn_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production spawn_detached_supervisor path fails closed when metadata write fails."""

    import subprocess
    import sys

    from ai_dev_loop.pr_review_v2.workers import spawn as spawn_mod
    from ai_dev_loop.pr_review_v2.workers.spawn import spawn_detached_supervisor

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock, prefix="p168-meta-fail")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    prep = PreparationService(engine, arts, clock=clock)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-meta-fail",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"patch\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx_for(
                repo_root=str(repo.resolve()), source_run_id="src-meta-fail"
            ),
        )
    )
    children: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen_sleep(*_args, **_kwargs):  # noqa: ANN002, ANN003
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        children.append(proc)
        return proc

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", popen_sleep)

    def failing_write(_metadata: SupervisorLauncherMetadata) -> Path:
        raise OSError("disk full")

    launchers.write = failing_write  # type: ignore[method-assign]
    try:
        action = spawn_detached_supervisor(
            created.run_id, artifact_root=arts.root, launcher_store=launchers
        )
        assert action == "spawn_failed"
        assert launchers.read(created.run_id) is None
        assert children, "Popen must have started before metadata persistence failed"
        for proc in children:
            assert proc.poll() is not None, "failed spawn must terminate the child"
    finally:
        for proc in children:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), 9)
                proc.wait(timeout=2)

    # Durable repair after reopen through the production spawner/launcher boundary.
    launchers.write = SupervisorLauncherStore.write.__get__(launchers, SupervisorLauncherStore)
    engine2 = reopen_engine(tmp_path / "engine.sqlite3", clock, prefix="p168-meta-fail")
    persist_ok = {"n": 0}
    real_write = launchers.write

    def counting_write(metadata: SupervisorLauncherMetadata) -> Path:
        persist_ok["n"] += 1
        return real_write(metadata)

    launchers.write = counting_write  # type: ignore[method-assign]
    control = ControlPlaneService(
        engine2,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=lambda rid: spawn_detached_supervisor(
            rid, artifact_root=arts.root, launcher_store=launchers
        ),
    )
    repaired_children: list[subprocess.Popen[bytes]] = []

    def popen_sleep_repair(*_args, **_kwargs):  # noqa: ANN002, ANN003
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        repaired_children.append(proc)
        return proc

    monkeypatch.setattr(spawn_mod.subprocess, "Popen", popen_sleep_repair)
    try:
        repaired = control.start(created.run_id)
        assert repaired.supervisor_action in {"spawned", "repaired"}
        meta = launchers.read(created.run_id)
        assert meta is not None
        assert persist_ok["n"] >= 1
        assert repaired_children, "repair spawn must launch a controlled child"
    finally:
        for proc in repaired_children:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(os.getpgid(proc.pid), 9)
                proc.wait(timeout=2)


def test_corrupt_and_reused_pid_metadata_is_not_treated_live(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock, prefix="p168-corrupt-pid")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    prep = PreparationService(engine, arts, clock=clock)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-corrupt-pid",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"patch\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx_for(
                repo_root=str(repo.resolve()), source_run_id="src-corrupt-pid"
            ),
        )
    )
    spawn_calls = {"n": 0}

    def spawner(_run_id: str) -> str:
        spawn_calls["n"] += 1
        return "spawned"

    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=spawner,
    )
    control.start(created.run_id)
    # Reused PID of this process, but wrong starttime/executable => not live.
    pid = os.getpid()
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=created.run_id,
            token="corrupt-token",
            pid=pid,
            pgid=os.getpgid(0),
            process_start_time="1",
            executable="/bin/false",
            created_at=clock.now().isoformat(),
        )
    )
    repaired = control.start(created.run_id)
    assert repaired.supervisor_action in {"repaired", "spawned"}
    assert spawn_calls["n"] == 2

"""Phase 16.8 control-plane, lease, timer, and abort ordering integration tests."""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    drive_to_waiting_for_bot,
    make_engine,
    make_observe_eligible,
)
from tests.integration.phase16_8_checkpoint_helpers import reopen_engine
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    NextActionCategory,
)
from ai_dev_loop.pr_review_v2.application.control import ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    AbortProcessAction,
    SafeNextAction,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.execution_context import (
    ExecutionContextArtifact,
    ExecutionContextCodex,
    ExecutionContextCursor,
    ExecutionContextPlanPrompt,
    ExecutionContextPrReviewV2,
    ExecutionContextRunBinding,
    ExecutionContextWorker,
    ExecutionContextWorkflow,
)
from ai_dev_loop.pr_review_v2.application.preparation import PreparationService, SourceRunSnapshot
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectRetryableFailure,
    ErrorSummary,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    TransientErrorKind,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.workers.owned_children import (
    OwnedChildStore,
    register_owned_child,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
)

PLAN_BYTES = b"frozen-plan-control\n"
PROMPT_BYTES = b"frozen-prompt-control\n"
PLAN_SHA = hashlib.sha256(PLAN_BYTES).hexdigest()
PROMPT_SHA = hashlib.sha256(PROMPT_BYTES).hexdigest()
SESSION = "11111111-1111-1111-1111-111111111111"
SHA_A = "a" * 40


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


def _ctx(*, repo_root: str) -> ExecutionContextArtifact:
    return ExecutionContextArtifact(
        run_binding=ExecutionContextRunBinding(
            prepared_from="source_run",
            source_run_id="src-ctrl",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
        ),
        cursor=ExecutionContextCursor(
            chat_id="chat-ctrl",
            model="composer-2.5-fast",
            command="agent",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        codex=ExecutionContextCodex(
            session_id=SESSION,
            review_model="gpt-5",
            review_reasoning_effort="medium",
            command="codex",
            sandbox="workspace-write",
            review_skill="review-staged-cursor-execution",
            external_review_skill="review-github-pr-feedback",
        ),
        workflow=ExecutionContextWorkflow(
            max_local_iterations=3,
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
        ),
        pr_review_v2=ExecutionContextPrReviewV2(
            gh_command="gh",
            git_command="git",
            ssh_command="ssh",
            remote_name="origin",
            reviewer_logins=("chatgpt-codex-connector",),
            review_trigger_body="@codex review",
            user_mention="rojobad",
            poll_interval_seconds=60,
            max_external_cycles=8,
            per_call_timeout_seconds=60,
            overall_timeout_seconds=180,
            max_pages=20,
            max_items=500,
            max_server_directed_wait_seconds=3600,
            no_findings_enabled=False,
            worker=ExecutionContextWorker(
                lease_ttl_seconds=30,
                heartbeat_interval_seconds=10,
                idle_poll_seconds=1,
            ),
        ),
        plan_prompt=ExecutionContextPlanPrompt(
            plan_path="plans/x.md",
            plan_sha256=PLAN_SHA,
            prompt_path="prompts/prompt.txt",
            prompt_sha256=PROMPT_SHA,
        ),
        repository_root=repo_root,
    )


def _control_stack(
    tmp_path: Path, clock: FakeClock
) -> tuple[PrReviewEngine, ControlPlaneService, str]:
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-ctrl")
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    calls: list[str] = []

    def spawner(run_id: str) -> str:
        calls.append(run_id)
        return "spawned"

    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=launchers,
        spawner=spawner,
    )
    prep = PreparationService(engine, arts, clock=clock)
    repo = tmp_path / "repo"
    repo.mkdir()
    created = prep.create_from_source(
        SourceRunSnapshot(
            source_run_id="src-ctrl",
            repository="acme/demo",
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch_bytes=b"diff --git a/x b/x\n",
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
            execution_context=_ctx(repo_root=str(repo.resolve())),
        )
    )
    return engine, control, created.run_id


def test_control_start_is_idempotent_repair(tmp_path: Path) -> None:
    clock = FakeClock()
    _engine, control, run_id = _control_stack(tmp_path, clock)
    started = control.start(run_id)
    assert started.transition_applied is True
    assert started.supervisor_action == "spawned"
    again = control.start(run_id)
    assert again.transition_applied is False
    assert again.supervisor_action in {"reused", "repaired", "spawned"}


def test_control_resume_from_paused(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    for attempt in range(1, 7):
        if attempt > 1:
            clock.advance(timedelta(seconds=31))
            status = engine.get_status(run_id)
            if status.next_eligible_at is not None:
                clock.set(status.next_eligible_at)
            engine.fire_due_timers_for_run(run_id)
        lease, claim = claim_next(engine, run_id)
        next_at = clock.now() + timedelta(seconds=30)
        receipt = engine.complete_claim(
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
        assert receipt.disposition is EventDisposition.ACCEPTED
        status = engine.get_status(run_id)
        if attempt < 6:
            assert status.state_kind == "waiting_retry"
        else:
            assert status.state_kind == "paused"
            break
    resumed = control.resume(run_id)
    assert resumed.transition_applied is True
    assert resumed.supervisor_action == "spawned"
    assert engine.get_status(run_id).state_kind != "paused"


def test_abort_during_claimed_observe_terminates_owned_child(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    with engine.store.begin_read() as conn:
        state, _version, _updated = engine.store.load_validated_snapshot(conn, run_id)
    prepared = PreparedState(
        run_id=run_id,
        origin=state.origin,
        limits=state.limits,
        entered_at=clock.now(),
    )
    drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, run_id, clock)
    lease, claim = claim_next(engine, run_id)
    assert claim.effect.kind == "observe_bot_review"

    arts = ProtectedResultStore(tmp_path / "artifacts")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        time.sleep(0.05)
        children = OwnedChildStore(arts.root)
        register_owned_child(
            children,
            run_id=run_id,
            component="codex",
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            executable=str(Path(sys.executable).resolve()),
        )
        result = control.abort(run_id)
        assert result.abort_persisted is True
        assert result.state_kind == "aborted"
        assert result.process_action is AbortProcessAction.TERMINATED
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), 9)
            proc.wait(timeout=2)
    del lease


def test_abort_persisted_before_process_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    with engine.store.begin_read() as conn:
        state, _version, _updated = engine.store.load_validated_snapshot(conn, run_id)
    prepared = PreparedState(
        run_id=run_id,
        origin=state.origin,
        limits=state.limits,
        entered_at=clock.now(),
    )
    drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, run_id, clock)
    claim_next(engine, run_id)

    arts = ProtectedResultStore(tmp_path / "artifacts")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    order: list[str] = []

    from ai_dev_loop.pr_review_v2.workers import owned_children as owned_children_mod

    original_signal = owned_children_mod.signal_owned_child

    def _signal_after_abort(child, *, run_id: str):  # noqa: ANN001
        with engine.store.begin_read() as conn:
            rows = conn.execute(
                """
                SELECT event_kind FROM pr_review_events
                WHERE run_id=? AND event_kind='abort_requested'
                """,
                (run_id,),
            ).fetchall()
        assert rows, "abort must be persisted before signaling owned child"
        order.append("signal")
        return original_signal(child, run_id=run_id)

    monkeypatch.setattr(owned_children_mod, "signal_owned_child", _signal_after_abort)
    try:
        time.sleep(0.05)
        children = OwnedChildStore(arts.root)
        register_owned_child(
            children,
            run_id=run_id,
            component="codex",
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            executable=str(Path(sys.executable).resolve()),
        )
        result = control.abort(run_id)
        assert result.abort_persisted is True
        assert order == ["signal"]
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), 9)
            proc.wait(timeout=2)


def test_two_worker_lease_fence_blocks_duplicate_mutating_write(tmp_path: Path) -> None:
    from dataclasses import dataclass, field

    from ai_dev_loop.pr_review_v2.application.write_contracts import (
        AuthorityLostError,
        ClaimAuthoritySnapshot,
        GitHubWritePolicy,
        WriteAuthorityStatus,
        WriteGatewaySuccess,
        claim_authority_snapshot_from_claim,
    )
    from ai_dev_loop.pr_review_v2.domain.events import CommitRecordedOutcome
    from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

    clock = FakeClock()
    engine = make_engine(tmp_path / "fence.sqlite3", clock, prefix="fence")
    prepared = PreparedState(
        run_id="run-fence",
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
    start_run(engine, prepared)

    class _EngineAuthority:
        def __init__(self, eng) -> None:  # noqa: ANN001
            self._engine = eng

        def check_authority(self, snapshot: ClaimAuthoritySnapshot):
            return self._engine.check_claim_authority(snapshot)

    @dataclass
    class _RecordingGateway:
        writes: list[str] = field(default_factory=list)

        def commit(self, effect, *, run_id, now, authorize):  # noqa: ANN001
            authorize()
            self.writes.append("commit")
            return WriteGatewaySuccess(
                outcome=CommitRecordedOutcome(
                    commit_sha="b" * 40,
                    new_head_sha="b" * 40,
                    expected_remote_sha_before_push=None,
                ),
                already_applied=False,
            )

    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert claim is not None and claim.classification == "local"
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="g1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    lease, claim = claim_next(engine, prepared.run_id)
    assert claim.classification == "mutating"

    clock.advance(timedelta(seconds=60))
    engine.acquire_lease(prepared.run_id, "owner-b")

    gateway = _RecordingGateway()
    executor = WriteExecutor(
        git_gateway=gateway,  # type: ignore[arg-type]
        github_gateway=gateway,  # type: ignore[arg-type]
        github_policy=GitHubWritePolicy(repository_cwd="/tmp/repo"),
    )
    with pytest.raises(AuthorityLostError):
        executor.execute(
            claim.effect,
            claim.completion_token,
            now=clock.now(),
            authority=_EngineAuthority(engine),
            claim=claim,
        )
    assert gateway.writes == []
    snapshot = claim_authority_snapshot_from_claim(claim)
    assert engine.check_claim_authority(snapshot).status is WriteAuthorityStatus.REJECTED


def test_future_timer_not_fired_early_on_sqlite_reopen(tmp_path: Path) -> None:
    clock = FakeClock()
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix="p168-timer")
    prepared = PreparedState(
        run_id="run-timer",
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
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="c1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    next_at = clock.now() + timedelta(seconds=45)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="c-retry",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="unavailable"),
                failed_attempt=claim.attempt,
                next_attempt_at=next_at,
            ),
        )
    )
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "waiting_retry"
    assert status.next_action is NextActionCategory.WAIT_FOR_RETRY
    engine2 = reopen_engine(db_path, clock, prefix="p168-timer-reopen")
    status2 = engine2.get_status(prepared.run_id)
    assert status2.state_kind == "waiting_retry"
    assert status2.next_eligible_at == next_at
    assert engine2.fire_due_timers_for_run(prepared.run_id) == []


def test_control_resume_from_active_repairs_supervisor(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    resumed = control.resume(run_id)
    assert resumed.transition_applied is False
    assert resumed.supervisor_action in {"reused", "repaired", "spawned", "spawn_failed"}


def test_control_resume_from_waiting_retry_without_early_timer(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    clock.advance(timedelta(seconds=31))
    status = engine.get_status(run_id)
    if status.next_eligible_at is not None:
        assert engine.fire_due_timers_for_run(run_id) == []
    lease, claim = claim_next(engine, run_id)
    next_at = clock.now() + timedelta(seconds=45)
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
                failed_attempt=claim.attempt,
                next_attempt_at=next_at,
            ),
        )
    )
    assert engine.get_status(run_id).state_kind == "waiting_retry"
    resumed = control.resume(run_id)
    assert resumed.supervisor_action in {"reused", "repaired", "spawned"}
    assert engine.fire_due_timers_for_run(run_id) == []


def test_control_resume_waiting_for_user_requires_confirmation(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.unit.pr_review_v2.helpers import artifact, reply_adjudication

    from ai_dev_loop.pr_review_v2.domain import (
        AdjudicationRecordedOutcome,
        EffectSucceeded,
        EligibleThreadsObservedOutcome,
        FrozenThreadSet,
    )
    from ai_dev_loop.pr_review_v2.domain.events import ThreadReplyConfirmedOutcome

    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    with engine.store.begin_read() as conn:
        state, _version, _updated = engine.store.load_validated_snapshot(conn, run_id)
    prepared = PreparedState(
        run_id=run_id,
        origin=state.origin,
        limits=state.limits,
        entered_at=clock.now(),
    )
    binding = drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, run_id, clock)
    lease, claim = claim_next(engine, run_id)
    with engine.store.begin_read() as conn:
        snap, _, _ = engine.store.load_validated_snapshot(conn, run_id)
    assert snap.trigger_evidence is not None
    frozen = FrozenThreadSet(
        thread_ids=("t1",),
        snapshot_ref=artifact("artifacts/threads.json"),
        head_sha=binding.head_sha,
        cycle_number=1,
        trigger_marker=snap.trigger_evidence.marker,
    )
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="obs-threads",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=EligibleThreadsObservedOutcome(frozen=frozen),
            ),
        )
    )
    lease, claim = claim_next(engine, run_id)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="adj-reply",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=AdjudicationRecordedOutcome(evidence=reply_adjudication(frozen)),
            ),
        )
    )
    assert engine.get_status(run_id).state_kind == "waiting_for_user"
    clock.advance(timedelta(seconds=31))
    status_pending = control.status(run_id)
    assert status_pending.next_action is SafeNextAction.RESUME
    resumed_pending = control.resume(run_id)
    assert resumed_pending.transition_applied is False
    assert resumed_pending.supervisor_action == "spawned"
    lease, reply_claim = claim_next(engine, run_id)
    assert reply_claim.effect.kind == "post_thread_reply"
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="post-reply",
            dispatch_id=reply_claim.dispatch_id,
            claim_id=reply_claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectSucceeded(
                occurred_at=clock.now(),
                token=reply_claim.completion_token,
                outcome=ThreadReplyConfirmedOutcome(
                    thread_id=reply_claim.effect.thread_id,
                    reply_ref=reply_claim.effect.reply_ref,
                ),
            ),
        )
    )
    assert engine.get_status(run_id).state_kind == "waiting_for_user"
    resumed = control.resume(run_id, confirm_user_continuation=True)
    assert resumed.transition_applied is True


def test_abort_signal_order_carrier_then_codex_then_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from ai_dev_loop.abort_control import register_active_process
    from ai_dev_loop.launcher import read_process_starttime
    from ai_dev_loop.pr_review_v2.application import control as control_mod

    clock = FakeClock()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    engine, control, run_id = _control_stack(tmp_path, clock)
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    control.start(run_id)

    carrier_run_id = "prv2c-abort-order"
    carrier_dir = tmp_path / "carrier-run"
    carrier_dir.mkdir(parents=True, exist_ok=True)
    (carrier_dir / "state.json").write_text('{"run_id":"prv2c-abort-order"}\n', encoding="utf-8")

    carrier_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    codex_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    supervisor_proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    time.sleep(0.05)
    carrier_pgid = os.getpgid(carrier_proc.pid)
    codex_pgid = os.getpgid(codex_proc.pid)
    supervisor_pgid = os.getpgid(supervisor_proc.pid)
    supervisor_start = read_process_starttime(supervisor_proc.pid)
    assert supervisor_start is not None

    register_active_process(
        carrier_dir,
        run_id=carrier_run_id,
        component="cursor",
        iteration=1,
        pid=carrier_proc.pid,
        pgid=carrier_pgid,
        parent_pid=os.getpid(),
        cwd=str(tmp_path),
        argv_redacted=["agent", "-p", "<redacted>"],
    )
    children = OwnedChildStore(arts.root)
    register_owned_child(
        children,
        run_id=run_id,
        component="codex",
        pid=codex_proc.pid,
        pgid=codex_pgid,
        executable=str(Path(sys.executable).resolve()),
    )
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=run_id,
            token="supervisor-token",
            pid=supervisor_proc.pid,
            pgid=supervisor_pgid,
            process_start_time=str(supervisor_start),
            executable=str(Path(sys.executable).resolve()),
            created_at=clock.now().isoformat(),
        )
    )

    signal_order: list[str] = []
    real_killpg = os.killpg
    abort_persisted_before_signal = {"ok": False}

    def _track_killpg(pgid: int, sig: int) -> None:
        if not abort_persisted_before_signal["ok"]:
            with engine.store.begin_read() as conn:
                rows = conn.execute(
                    """
                    SELECT event_kind FROM pr_review_events
                    WHERE run_id=? AND event_kind='abort_requested'
                    """,
                    (run_id,),
                ).fetchall()
            assert rows, "abort must be persisted before signaling owned processes"
            abort_persisted_before_signal["ok"] = True
        if int(sig) == int(signal.SIGTERM):
            if pgid == carrier_pgid:
                signal_order.append("carrier")
            elif pgid == codex_pgid:
                signal_order.append("codex")
            elif pgid == supervisor_pgid:
                signal_order.append("supervisor")
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", _track_killpg)
    import ai_dev_loop.run_discovery as run_discovery_mod

    original_find = run_discovery_mod.find_run_directory

    def _find_run_directory(rid: str) -> Path:
        if rid == carrier_run_id:
            return carrier_dir
        return original_find(rid)

    monkeypatch.setattr(run_discovery_mod, "find_run_directory", _find_run_directory)
    monkeypatch.setattr(
        control_mod.ControlPlaneService,
        "_capture_local_fix_carrier_id",
        lambda self, rid: carrier_run_id,  # noqa: ARG005
    )

    try:
        result = control.abort(run_id)
        assert result.abort_persisted is True
        assert abort_persisted_before_signal["ok"] is True
        assert signal_order == ["carrier", "codex", "supervisor"]
    finally:
        for proc in (carrier_proc, codex_proc, supervisor_proc):
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=2)


def test_abort_supervisor_ownership_mismatch_is_not_signaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    arts = ProtectedResultStore(tmp_path / "artifacts")
    launchers = SupervisorLauncherStore(arts.root)
    control.start(run_id)
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=run_id,
            token="supervisor-token",
            pid=999999,
            pgid=999999,
            process_start_time="0",
            executable="/bin/false",
            created_at=clock.now().isoformat(),
        )
    )
    signals: list[int] = []
    real_killpg = os.killpg

    def _track_killpg(pgid: int, sig: int) -> None:
        if int(sig) in {int(signal.SIGTERM), int(signal.SIGKILL)}:
            signals.append(int(sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", _track_killpg)
    result = control.abort(run_id)
    assert result.abort_persisted is True
    assert signals == []


def test_abort_one_child_signal_failure_still_preserves_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.pr_review_v2.workers import owned_children as owned_children_mod

    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    repo = tmp_path / "repo"
    marker = repo / "preserve.txt"
    marker.write_text("keep\n", encoding="utf-8")
    control.start(run_id)
    arts = ProtectedResultStore(tmp_path / "artifacts")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
    )
    time.sleep(0.05)
    register_owned_child(
        OwnedChildStore(arts.root),
        run_id=run_id,
        component="codex",
        pid=proc.pid,
        pgid=os.getpgid(proc.pid),
        executable=str(Path(sys.executable).resolve()),
    )

    def _refuse_signal(child, *, run_id: str):  # noqa: ANN001
        del child, run_id
        return "refused"

    monkeypatch.setattr(owned_children_mod, "signal_owned_child", _refuse_signal)
    result = control.abort(run_id)
    assert result.abort_persisted is True
    assert result.state_kind == "aborted"
    assert marker.read_text(encoding="utf-8") == "keep\n"
    if proc.poll() is None:
        os.killpg(os.getpgid(proc.pid), 9)
        proc.wait(timeout=2)


def test_abort_late_completion_is_fenced(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    lease, claim = claim_next(engine, run_id)
    control.abort(run_id)
    from tests.integration.phase16_8_checkpoint_helpers import assert_completion_fenced

    assert_completion_fenced(
        engine,
        lease,
        claim,
        publication_success(claim.effect, claim.completion_token, clock.now()),
    )


def test_abort_preserves_repository_contents(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    repo = tmp_path / "repo"
    marker = repo / "preserve.txt"
    marker.write_text("keep\n", encoding="utf-8")
    before = marker.read_text(encoding="utf-8")
    control.start(run_id)
    control.abort(run_id)
    assert marker.read_text(encoding="utf-8") == before


def test_stale_owned_child_metadata_is_not_signaled(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.workers.owned_children import OwnedChildMetadata

    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    arts = ProtectedResultStore(tmp_path / "artifacts")
    children = OwnedChildStore(arts.root)
    children.write(
        OwnedChildMetadata(
            schema_version=1,
            run_id=run_id,
            component="codex",
            pid=999999,
            pgid=999999,
            process_start_time="0",
            executable="/bin/false",
            parent_pid=1,
            created_at=clock.now().isoformat(),
        )
    )
    result = control.abort(run_id)
    assert result.abort_persisted is True
    assert result.state_kind == "aborted"


def test_status_dead_supervisor_expired_lease_recommends_resume(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    # Spawner returned "spawned" but no live launcher metadata => dead supervisor.
    assert control._supervisor_live(run_id) is False  # noqa: SLF001
    status = control.status(run_id)
    assert status.state_kind == "publishing_initial"
    assert status.lease_active is False
    assert status.supervisor_live is False
    assert status.resumable is True
    assert status.next_action is SafeNextAction.RESUME
    resumed = control.resume(run_id)
    assert resumed.transition_applied is False
    assert resumed.supervisor_action in {"spawned", "repaired"}
    with engine.store.begin_read() as conn:
        starts = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM pr_review_events "
                "WHERE run_id=? AND event_kind='start_requested'",
                (run_id,),
            ).fetchone()["n"]
        )
    assert starts == 1


def test_status_dead_supervisor_active_lease_does_not_race(tmp_path: Path) -> None:
    from ai_dev_loop.pr_review_v2.application.contracts import LeaseStatus

    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    lease = engine.acquire_lease(run_id, "lingering-owner")
    assert lease.status is LeaseStatus.ACTIVE
    status = control.status(run_id)
    assert status.supervisor_live is False
    assert status.lease_active is True
    assert status.resumable is False
    assert status.next_action is SafeNextAction.WAIT_UNTIL
    engine.release_lease(run_id, "lingering-owner", lease.generation)
    after = control.status(run_id)
    assert after.lease_active is False
    assert after.resumable is True
    assert after.next_action is SafeNextAction.RESUME


def test_status_paused_waiting_user_and_terminal_semantics(tmp_path: Path) -> None:
    clock = FakeClock()
    engine, control, run_id = _control_stack(tmp_path, clock)
    control.start(run_id)
    for attempt in range(1, 7):
        if attempt > 1:
            clock.advance(timedelta(seconds=31))
            status = engine.get_status(run_id)
            if status.next_eligible_at is not None:
                clock.set(status.next_eligible_at)
            engine.fire_due_timers_for_run(run_id)
        lease, claim = claim_next(engine, run_id)
        next_at = clock.now() + timedelta(seconds=30)
        engine.complete_claim(
            EffectCompletionRequest(
                submission_id=f"fail-status-{attempt}",
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
        if engine.get_status(run_id).state_kind == "paused":
            break
    paused = control.status(run_id)
    assert paused.state_kind == "paused"
    assert paused.resumable is True
    assert paused.next_action is SafeNextAction.RESUME

    aborted = control.abort(run_id)
    assert aborted.state_kind == "aborted"
    terminal = control.status(run_id)
    assert terminal.state_kind == "aborted"
    assert terminal.resumable is False
    assert terminal.next_action is SafeNextAction.NONE

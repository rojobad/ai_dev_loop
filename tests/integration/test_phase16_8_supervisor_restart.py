"""Phase 16.8 supervisor, timer, lease, and control-path production-boundary tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    drive_to_waiting_for_bot,
    make_engine,
    make_observe_eligible,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    StartRequested,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import PrReviewV2Supervisor
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
RUN_ID = "run-sup-timer"


def test_supervisor_waits_for_future_retry_timer(tmp_path: Path) -> None:
    clock = FakeClock(T0)
    sleeps: list[float] = []

    class _Engine:
        def __init__(self) -> None:
            self._kind = "waiting_retry"
            self._due = T0 + timedelta(seconds=5)
            self.fired = 0

        def fire_due_timers_for_run(self, run_id: str, *, limit: int = 100) -> list:
            del run_id, limit
            self.fired += 1
            if clock.now() >= self._due:
                self._kind = "paused"
            return []

        def get_status(self, run_id: str):
            del run_id

            class S:
                state_kind = self._kind
                next_eligible_at = self._due if self._kind == "waiting_retry" else None
                lease_active = False
                active_effect_kind = None

            return S()

    class _Worker:
        def run_once(self, run_id: str):
            del run_id

            class R:
                claimed = False

            return R()

    cancel = {"n": 0}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))
        cancel["n"] += 1

    engine = _Engine()
    supervisor = PrReviewV2Supervisor(
        engine,  # type: ignore[arg-type]
        _Worker(),  # type: ignore[arg-type]
        run_id=RUN_ID,
        idle_poll_seconds=1.0,
        clock=clock,
        cancel_check=lambda: cancel["n"] > 10,
        sleep=sleep,
    )
    kind = supervisor.run_until_idle()
    assert kind in {"paused", "waiting_retry"}
    assert sleeps
    assert all(seconds <= 1.0 for seconds in sleeps)
    assert engine.fired >= 1


def test_expired_readonly_claim_requeues_same_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock)
    prepared = PreparedState(
        run_id="run-16-5-lease",
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256="1" * 64),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json",
                sha256="2" * 64,
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=clock.now(),
    )
    engine.create_run(prepared.run_id, prepared)
    engine.apply_event(
        EventSubmission(
            submission_id="start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, prepared.run_id, clock)
    lease, claim = claim_next(engine, prepared.run_id)
    assert claim.effect.kind == "observe_bot_review"
    dispatch_id = claim.dispatch_id
    effect_id = claim.effect.effect_id
    clock.advance(timedelta(seconds=31))
    engine2 = make_engine(tmp_path / "db.sqlite3", clock, prefix="reopen")
    lease2 = engine2.acquire_lease(prepared.run_id, "owner-b")
    engine2.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    result = engine2.claim_next_effect(prepared.run_id, "owner-b", lease2.generation)
    assert result.claim is not None
    assert result.claim.dispatch_id == dispatch_id
    assert result.claim.effect.effect_id == effect_id


def test_lease_replacement_fences_stale_mutating_write(tmp_path: Path) -> None:
    from dataclasses import dataclass, field

    from tests.unit.pr_review_v2.durable_helpers import publication_success, start_run

    from ai_dev_loop.pr_review_v2.application.contracts import EffectCompletionRequest
    from ai_dev_loop.pr_review_v2.application.write_contracts import (
        AuthorityLostError,
        ClaimAuthoritySnapshot,
        GitHubWritePolicy,
        WriteAuthorityStatus,
        WriteGatewaySuccess,
        claim_authority_snapshot_from_claim,
    )
    from ai_dev_loop.pr_review_v2.domain.events import CommitRecordedOutcome

    clock = FakeClock()
    engine = make_engine(tmp_path / "fence.sqlite3", clock, prefix="fence")
    prepared = PreparedState(
        run_id="run-1",
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
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


def test_stale_completion_after_lease_replacement_is_fenced(tmp_path: Path) -> None:
    from tests.integration.test_phase16_6_crash_restart_and_fencing import (
        test_stale_completion_after_replacement_is_fenced,
    )

    test_stale_completion_after_replacement_is_fenced(tmp_path)


def test_future_timer_not_fired_early_on_sqlite_reopen(tmp_path: Path, monkeypatch) -> None:
    from tests.integration.phase16_8_checkpoint_helpers import reopen_engine
    from tests.unit.pr_review_v2.durable_helpers import publication_success, start_run

    from ai_dev_loop.pr_review_v2.application.contracts import (
        EffectCompletionRequest,
        NextActionCategory,
    )
    from ai_dev_loop.pr_review_v2.domain import (
        EffectRetryableFailure,
        ErrorSummary,
        TransientErrorKind,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
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
            expected_head_sha="a" * 40,
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
